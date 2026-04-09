"""Director service — the super-agent that communicates with the user.

The Director has a persistent message history (stored in DirectorSession)
so it maintains context across many conversation turns.  It reads everything,
plans via manage_todo, and delegates wiki changes to specialist agents via
create_agent_task + trigger_pipeline.

It deliberately cannot propose/review/apply pages itself — all mutations must
go through the normal specialist pipeline.

Public API:
    create_session(db, title) -> DirectorSession
    get_session(db, session_id) -> DirectorSession | None
    list_sessions(db) -> list[DirectorSession]
    delete_session(db, session_id) -> bool

    run_director_turn(db, session, user_message, on_event=None) -> str
        Runs one conversation turn.  on_event(event_dict) is called
        synchronously for each tool-call / result event so callers
        can stream progress to the user.

Event dict shapes:
    {"type": "thinking",    "iteration": int}
    {"type": "tool_call",   "tool": str, "args": dict}
    {"type": "tool_result", "tool": str, "ok": bool, "preview": str}
    {"type": "delegate",    "agent_type": str, "instruction": str}
    {"type": "pipeline",    "pipeline_type": str}
    {"type": "reply",       "text": str}
    {"type": "error",       "message": str}
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.debug.event_stream import debug_stream
from core.models.director import DirectorSession
from core.services.llm_service import get_client, resolve_model
from core.settings import settings
from core.tools.definitions import get_tools_for_agent
from core.tools.handlers import dispatch_tool

logger = logging.getLogger(__name__)


# ── system prompt ─────────────────────────────────────────────────────────────

_DIRECTOR_PROMPT = """\
You are the Director — the high-level coordinator of the Wolfina Wiki knowledge system.
Your role is to communicate with the user, understand their goals, and orchestrate the
multi-agent pipeline to fulfil them.

== YOUR RESPONSIBILITIES ==
1. UNDERSTAND: Listen carefully to what the user wants. Ask clarifying questions if needed.
2. PLAN: Use manage_todo to maintain a running plan visible across turns.
3. RESEARCH: Use read-only tools (search_pages, get_page, read_file, list_files, spawn_subagents)
   to gather information before acting.
4. DELEGATE: Assign work to specialist agents via create_agent_task.
   - agent_type "research"  → deep research and fact-gathering
   - agent_type "proposer"  → write and submit wiki page proposals
   - agent_type "reviewer"  → review and approve/reject proposals
   - agent_type "executor"  → apply approved proposals to the wiki
   - agent_type "relation"  → add semantic links between pages
5. TRIGGER: After creating tasks, call trigger_pipeline(pipeline_type="maintenance")
   so the specialist agents actually run.
6. MONITOR: Use list_agent_tasks and list_proposals to track delegated work.
7. REPORT: Tell the user clearly what you've done, what's in progress, and what's done.

== WHAT YOU MUST NOT DO ==
- Do NOT call propose_new_page, propose_page_edit, review_proposal, apply_proposal,
  or add_page_relation directly — these are reserved for specialist agents.
- Do NOT try to do the work yourself. Always delegate substantive wiki changes.
- Do NOT create tasks without triggering the pipeline to process them.

== TOOL USAGE TIPS ==
- manage_todo: Keep a clear plan. Add items when you commit to doing something;
  mark them complete when the pipeline confirms the work is queued or done.
- trigger_pipeline: After delegating tasks, always trigger "maintenance".
  If files need ingesting, trigger "ingest" or "ingest_scan" first.
- spawn_subagents: Use for parallel research when you need information from
  multiple sources at once.
- quick_query: Ask a focused question and get a concise summarised answer back
  in one call.  The query agent searches the wiki and files internally, so you
  don't have to page through raw results yourself.
  Required: query (string) — be specific about what you want.
  Optional: summary_instruction — e.g. "List key facts as bullet points."
  Optional: max_words (10–800, default 150) — hard limit on the summary length.
  Optional: allowed_tools — restrict which read tools the query agent may use;
    valid values: search_pages, get_page, list_pages, get_related_pages,
    get_page_history, read_file, list_files.
  Returns: {"summary": "...", "sources": ["slug-or-path", ...]}
  Use quick_query instead of chaining multiple search_pages / get_page calls
  when you just need a quick factual answer.
- list_agent_tasks(status="pending"): Check what's still waiting to be processed.
- list_proposals(status="pending"): See proposals awaiting review.

== TODO LIST ==
Use manage_todo to track active work items. The list holds up to 10 items.
When it is full, use manage_note to record deferred tasks instead of silently dropping them.
Mark items complete as soon as the corresponding pipeline work is queued or confirmed done.

== NOTES ==
Use manage_note only after hitting a dead-end — an error or a condition that blocks you
from completing a task right now. Do not use it proactively as a planning tool.
Common triggers: manage_todo returns a capacity error; a tool call fails and cannot be
retried immediately; a task is blocked on something outside your control.
Every active note is injected into your context under "ACTIVE NOTES" at the start of each
turn. Resolve a note once its completion_criteria are satisfied.

== CONTEXT WINDOW ==
Your full conversation history with the user is stored and restored on every turn.
You have long-term memory of everything discussed in this session.
"""


# ── session CRUD ──────────────────────────────────────────────────────────────

async def create_session(db: AsyncSession, title: str = "New Session") -> DirectorSession:
    session = DirectorSession(
        id=str(uuid.uuid4()),
        title=title,
        messages="[]",
        todo_list="[]",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session(db: AsyncSession, session_id: str) -> DirectorSession | None:
    return await db.scalar(
        select(DirectorSession).where(DirectorSession.id == session_id)
    )


async def list_sessions(db: AsyncSession, limit: int = 50) -> list[DirectorSession]:
    result = await db.scalars(
        select(DirectorSession)
        .order_by(DirectorSession.updated_at.desc())
        .limit(min(limit, 200))
    )
    return list(result.all())


async def delete_session(db: AsyncSession, session_id: str) -> bool:
    session = await get_session(db, session_id)
    if session is None:
        return False
    await db.delete(session)
    await db.commit()
    return True


MAX_TODO_ITEMS = 10


# ── manage_todo (handled here, not in dispatch_tool) ─────────────────────────

def _handle_manage_todo(inp: dict, todo_list: list[dict]) -> dict:
    """Mutates todo_list in-place and returns result dict."""
    action = inp.get("action", "list")

    if action == "list":
        return {"todo_list": todo_list, "count": len(todo_list)}

    if action == "add":
        item_text = inp.get("item", "").strip()
        if not item_text:
            return {"error": "item text is required for action='add'"}
        active_count = sum(1 for t in todo_list if not t["done"])
        if active_count >= MAX_TODO_ITEMS:
            return {
                "error": (
                    f"Todo list is full ({active_count}/{MAX_TODO_ITEMS} active items). "
                    "Use manage_note to record this as a deferred note instead, "
                    "or complete/remove existing items first."
                )
            }
        new_id = max((t["id"] for t in todo_list), default=0) + 1
        entry = {
            "id": new_id,
            "text": item_text,
            "done": False,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        todo_list.append(entry)
        return {"added": entry, "todo_list": todo_list}

    if action == "complete":
        item_id = inp.get("item_id")
        if item_id is None:
            return {"error": "item_id is required for action='complete'"}
        for t in todo_list:
            if t["id"] == int(item_id):
                t["done"] = True
                return {"completed": t, "todo_list": todo_list}
        return {"error": f"Todo item {item_id} not found."}

    if action == "remove":
        item_id = inp.get("item_id")
        if item_id is None:
            return {"error": "item_id is required for action='remove'"}
        before = len(todo_list)
        todo_list[:] = [t for t in todo_list if t["id"] != int(item_id)]
        removed = before - len(todo_list)
        return {"removed": removed, "todo_list": todo_list}

    return {"error": f"Unknown action: {action}"}


# ── manage_note (handled here, not in dispatch_tool) ─────────────────────────

def _handle_manage_note(inp: dict, notes: list[dict]) -> dict:
    """Mutates notes in-place and returns result dict."""
    action = inp.get("action", "list")

    if action == "list":
        active = [n for n in notes if not n["resolved"]]
        tag_filter = inp.get("tag_filter", "").strip().lower()
        if tag_filter:
            active = [n for n in active if any(tag_filter in t.lower() for t in n["tags"])]
        return {"notes": active, "count": len(active)}

    if action == "write":
        body = inp.get("body", "").strip()
        if not body:
            return {"error": "body is required for action='write'"}
        new_id = max((n["id"] for n in notes), default=0) + 1
        entry = {
            "id": new_id,
            "body": body,
            "completion_criteria": inp.get("completion_criteria", "").strip(),
            "tags": [t.strip() for t in (inp.get("tags") or []) if t.strip()],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "resolved": False,
        }
        notes.append(entry)
        return {"written": entry, "active_count": sum(1 for n in notes if not n["resolved"])}

    if action == "resolve":
        note_id = inp.get("note_id")
        if note_id is None:
            return {"error": "note_id is required for action='resolve'"}
        for n in notes:
            if n["id"] == int(note_id):
                n["resolved"] = True
                n["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                return {"resolved": n, "active_count": sum(1 for n2 in notes if not n2["resolved"])}
        return {"error": f"Note {note_id} not found."}

    return {"error": f"Unknown action: {action}"}


def _build_notes_context(notes: list[dict]) -> str:
    """Return a system-prompt section for active notes, or empty string if none."""
    active = [n for n in notes if not n["resolved"]]
    if not active:
        return ""
    lines = ["\n== ACTIVE NOTES (deferred tasks — review and act when conditions are met) =="]
    for n in active:
        lines.append(f"\n[Note #{n['id']}] Created: {n['created_at']}")
        if n["tags"]:
            lines.append(f"  Tags: {', '.join(n['tags'])}")
        lines.append(f"  Task: {n['body']}")
        if n.get("completion_criteria"):
            lines.append(f"  Done when: {n['completion_criteria']}")
    lines.append("")
    return "\n".join(lines)


# ── main conversation turn ────────────────────────────────────────────────────

async def run_director_turn(
    db: AsyncSession,
    session: DirectorSession,
    user_message: str,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Execute one user turn for the director.

    Loads session history, runs the tool loop, saves updated history and
    todo list back to the session, and returns the final reply text.

    on_event is called synchronously with event dicts at each step so the
    caller (SSE handler or demo) can stream progress in real-time.
    """

    def emit(evt: dict[str, Any]) -> None:
        debug_stream.emit("director_" + evt["type"], **{k: v for k, v in evt.items() if k != "type"})
        if on_event:
            on_event(evt)

    # Load persistent state
    messages: list[dict] = json.loads(session.messages or "[]")
    todo_list: list[dict] = json.loads(session.todo_list or "[]")
    notes: list[dict] = json.loads(session.notes or "[]")

    # Build system prompt — inject active notes so they're always visible
    notes_context = _build_notes_context(notes)
    system_prompt = _DIRECTOR_PROMPT + notes_context

    # Build the full message list for this turn
    history: list[dict] = (
        [{"role": "system", "content": system_prompt}]
        + messages
        + [{"role": "user", "content": user_message}]
    )

    client = get_client()
    model = resolve_model("director")
    tools = get_tools_for_agent("director")
    is_ollama = settings.llm_provider != "openai_compat"
    max_iter = settings.agent_max_iterations

    final_text = ""
    tool_results: list[dict[str, Any]] = []
    # Track new messages added this turn (to persist)
    new_messages: list[dict] = [{"role": "user", "content": user_message}]

    for iteration in range(max_iter):
        emit({"type": "thinking", "iteration": iteration})
        resp = await client.chat(model=model, messages=history, tools=tools)

        if resp.finish_reason == "error":
            emit({"type": "error", "message": f"LLM error on iteration {iteration}"})
            break

        if resp.finish_reason != "tool_calls" or not resp.tool_calls:
            final_text = (resp.text or "").strip()
            break

        # Build assistant tool-call message
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.arguments if is_ollama else json.dumps(tc.arguments),
                },
            }
            for tc in resp.tool_calls
        ]
        history.append(assistant_msg)
        new_messages.append(assistant_msg)

        # Execute tools
        for tc in resp.tool_calls:
            emit({"type": "tool_call", "tool": tc.name, "args": tc.arguments})

            # Special: detect delegation intent for richer event
            if tc.name == "create_agent_task":
                emit({
                    "type": "delegate",
                    "agent_type": tc.arguments.get("agent_type", "?"),
                    "instruction": tc.arguments.get("instruction", "")[:200],
                })
            elif tc.name == "trigger_pipeline":
                emit({
                    "type": "pipeline",
                    "pipeline_type": tc.arguments.get("pipeline_type", "?"),
                })

            # Handle manage_todo / manage_note locally (need in-memory list references)
            if tc.name == "manage_todo":
                result = _handle_manage_todo(tc.arguments, todo_list)
            elif tc.name == "manage_note":
                result = _handle_manage_note(tc.arguments, notes)
            else:
                result = await dispatch_tool(tc.name, tc.arguments, db)

            ok = "error" not in result
            result_str = json.dumps(result, ensure_ascii=False, default=str)
            emit({
                "type": "tool_result",
                "tool": tc.name,
                "ok": ok,
                "preview": result_str[:200] + ("…" if len(result_str) > 200 else ""),
            })
            tool_results.append({"tool": tc.name, "ok": ok, "result": result})

            tool_msg: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": tc.id,
                "name": tc.name,
                "content": result_str,
            }
            history.append(tool_msg)
            new_messages.append(tool_msg)

    else:
        logger.warning("Director reached max_iterations (%d)", max_iter)

    if not final_text:
        # Some providers can end a tool loop without producing a final assistant
        # message. Force one non-tool synthesis turn so users always get a reply
        # based on concrete tool outcomes from this turn.
        forced_history = history + [{
            "role": "system",
            "content": (
                "You already have all tool outputs for this turn. "
                "Do NOT call tools now. Reply to the user immediately with: "
                "(1) what was completed, (2) what failed, and (3) next steps."
            ),
        }]
        forced = await client.chat(model=model, messages=forced_history, tools=[])
        final_text = (forced.text or "").strip()

    if not final_text:
        # Last-resort fallback to avoid silent turns in the CLI/SSE stream.
        failed = [r for r in tool_results if not r["ok"]]
        succeeded = [r["tool"] for r in tool_results if r["ok"]]
        if failed:
            failed_tools = ", ".join(sorted({r["tool"] for r in failed}))
            final_text = (
                "我已執行工具，但有部分步驟失敗。"
                f"失敗工具：{failed_tools}。"
                "請讓我重試或提供更具體條件，我會繼續完成。"
            )
        elif succeeded:
            done_tools = ", ".join(sorted(set(succeeded)))
            final_text = (
                "我已完成這一輪工具執行並取得結果，"
                f"涉及工具：{done_tools}。"
                "如需我繼續下一步，我可以直接接續處理。"
            )
        else:
            final_text = "我已收到你的請求，但這一輪沒有成功產生可回覆內容。請再試一次。"

    # Append final assistant reply to persistent history
    if final_text:
        reply_msg = {"role": "assistant", "content": final_text}
        new_messages.append(reply_msg)

    # Persist updated state
    messages.extend(new_messages)
    session.messages = json.dumps(messages)
    session.todo_list = json.dumps(todo_list)
    session.notes = json.dumps(notes)
    session.updated_at = datetime.now(timezone.utc)
    await db.commit()

    emit({"type": "reply", "text": final_text})
    return final_text
