"""Tool handler dispatch layer.

Each handler receives the tool input dict and a live AsyncSession,
calls the appropriate core service, and returns a plain dict that will be
JSON-serialised and sent back to the LLM as a tool_result.

dispatch_tool() is the single entry point used by the LLM service.
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.debug.event_stream import debug_stream
from core.settings import settings
from core.models.conversation import AgentTask, TaskStatus
from core.schemas.page import PageSearchParams, RelationCreate, SortField, SortOrder
from core.schemas.proposal import ApplyRequest, ProposalCreate, ReviewRequest
from core.services import page_service, proposal_service, version_service

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _page_out(page) -> dict:
    return {
        "id": page.id,
        "title": page.title,
        "slug": page.slug,
        "summary": page.summary,
        "content": page.content,
        "status": page.status,
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
    }


def _proposal_out(p) -> dict:
    return {
        "id": p.id,
        "target_page_id": p.target_page_id,
        "proposed_title": p.proposed_title,
        "proposed_summary": p.proposed_summary,
        "rationale": p.rationale,
        "proposer_agent_id": p.proposer_agent_id,
        "status": p.status,
        "batch_id": p.batch_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "reviews": [
            {
                "reviewer_agent_id": r.reviewer_agent_id,
                "decision": r.decision,
                "feedback": r.feedback,
            }
            for r in (p.reviews or [])
        ],
    }


def _task_out(t: AgentTask) -> dict:
    return {
        "id": t.id,
        "agent_type": t.agent_type,
        "instruction": t.instruction,
        "context_json": t.context_json,
        "status": t.status,
        "batch_id": t.batch_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


# ── individual handlers ───────────────────────────────────────────────────────

async def _search_pages(inp: dict, db: AsyncSession) -> dict:
    params = PageSearchParams(
        q=inp["query"],
        status=inp.get("status"),
        limit=min(int(inp.get("limit", 10)), 50),
    )
    pages = await page_service.search_pages(db, params)
    return {"pages": [_page_out(p) for p in pages]}


async def _get_page(inp: dict, db: AsyncSession) -> dict:
    if inp.get("page_id"):
        page = await page_service.get_page(db, inp["page_id"])
    elif inp.get("slug"):
        page = await page_service.get_page_by_slug(db, inp["slug"])
    else:
        return {"error": "Provide either page_id or slug."}
    return {"page": _page_out(page)}


async def _list_pages(inp: dict, db: AsyncSession) -> dict:
    sort_map = {"updated_at": SortField.updated_at, "created_at": SortField.created_at, "title": SortField.title}
    order_map = {"asc": SortOrder.asc, "desc": SortOrder.desc}
    params = PageSearchParams(
        status=inp.get("status"),
        sort_by=sort_map.get(inp.get("sort_by", "updated_at"), SortField.updated_at),
        sort_order=order_map.get(inp.get("sort_order", "desc"), SortOrder.desc),
        limit=min(int(inp.get("limit", 20)), 100),
        offset=int(inp.get("offset", 0)),
    )
    pages = await page_service.search_pages(db, params)
    return {"pages": [_page_out(p) for p in pages]}


async def _get_related_pages(inp: dict, db: AsyncSession) -> dict:
    pages = await page_service.get_related_pages(db, inp["page_id"])
    return {"related_pages": [_page_out(p) for p in pages]}


async def _compare_pages(inp: dict, db: AsyncSession) -> dict:
    """Return side-by-side title/summary/content snippet for multiple pages."""
    page_ids = inp.get("page_ids") or []
    if not page_ids or len(page_ids) < 2:
        return {"error": "Provide at least 2 page_ids to compare."}
    results = []
    for pid in page_ids:
        try:
            page = await page_service.get_page(db, pid)
            snippet = (page.content or "")[:400]
            results.append({
                "id": page.id,
                "title": page.title,
                "slug": page.slug,
                "summary": page.summary,
                "content_snippet": snippet + ("…" if len(page.content or "") > 400 else ""),
                "status": page.status,
                "updated_at": page.updated_at.isoformat() if page.updated_at else None,
            })
        except Exception as exc:
            results.append({"id": pid, "error": str(exc)})
    return {"pages": results}


async def _get_page_history(inp: dict, db: AsyncSession) -> dict:
    versions = await version_service.get_history(db, inp["page_id"])
    return {
        "versions": [
            {
                "version_number": v.version_number,
                "title": v.title,
                "summary": v.summary,
                "editor_agent_id": v.editor_agent_id,
                "edit_reason": v.edit_reason,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in versions
        ]
    }


async def _list_proposals(inp: dict, db: AsyncSession) -> dict:
    from core.models.proposal import ProposalStatus
    status = None
    if inp.get("status"):
        try:
            status = ProposalStatus(inp["status"])
        except ValueError:
            return {"error": f"Unknown status: {inp['status']}"}
    proposals = await proposal_service.list_proposals(
        db,
        page_id=inp.get("page_id"),
        status=status,
        batch_id=inp.get("batch_id"),
        limit=min(int(inp.get("limit", 20)), 100),
    )
    return {"proposals": [_proposal_out(p) for p in proposals]}


async def _propose_new_page(inp: dict, db: AsyncSession) -> dict:
    source_refs = inp.get("source_refs")
    data = ProposalCreate(
        proposed_title=inp["title"],
        proposed_slug=inp["slug"],
        proposed_content=inp["content"],
        proposed_summary=inp["summary"],
        proposed_source_refs=json.dumps(source_refs) if source_refs else None,
        rationale=inp["rationale"],
        proposer_agent_id=inp["proposer_agent_id"],
        batch_id=inp.get("batch_id"),
        idempotency_key=inp.get("idempotency_key"),
    )
    proposal = await proposal_service.create_proposal(db, data)
    debug_stream.emit("proposal_created", proposal_id=proposal.id,
                      title=proposal.proposed_title, proposer=proposal.proposer_agent_id,
                      batch_id=proposal.batch_id)
    return {"proposal": _proposal_out(proposal)}


async def _propose_page_edit(inp: dict, db: AsyncSession) -> dict:
    if not inp.get("target_page_id"):
        return {"error": "target_page_id is required for propose_page_edit. Use get_page or search_pages to find the page UUID first."}
    source_refs = inp.get("source_refs")
    data = ProposalCreate(
        target_page_id=inp["target_page_id"],
        proposed_title=inp.get("proposed_title"),
        proposed_content=inp["proposed_content"],
        proposed_summary=inp["proposed_summary"],
        proposed_source_refs=json.dumps(source_refs) if source_refs else None,
        rationale=inp["rationale"],
        proposer_agent_id=inp["proposer_agent_id"],
        batch_id=inp.get("batch_id"),
        idempotency_key=inp.get("idempotency_key"),
    )
    proposal = await proposal_service.create_proposal(db, data)
    debug_stream.emit("proposal_created", proposal_id=proposal.id,
                      title=proposal.proposed_title, proposer=proposal.proposer_agent_id,
                      batch_id=proposal.batch_id)
    return {"proposal": _proposal_out(proposal)}


async def _review_proposal(inp: dict, db: AsyncSession) -> dict:
    from core.models.proposal import ReviewDecision
    # Normalise common LLM mistakes: "approved"→"approve", "rejected"→"reject"
    raw_decision = inp["decision"].strip().lower()
    if raw_decision == "approved":
        raw_decision = "approve"
    elif raw_decision == "rejected":
        raw_decision = "reject"
    data = ReviewRequest(
        reviewer_agent_id=inp["reviewer_agent_id"],
        decision=ReviewDecision(raw_decision),
        feedback=inp.get("feedback"),
    )
    proposal = await proposal_service.review_proposal(db, inp["proposal_id"], data)
    debug_stream.emit("proposal_reviewed", proposal_id=proposal.id,
                      decision=inp["decision"], reviewer=inp["reviewer_agent_id"],
                      status=proposal.status)
    return {"proposal": _proposal_out(proposal)}


async def _apply_proposal(inp: dict, db: AsyncSession) -> dict:
    data = ApplyRequest(executor_agent_id=inp["executor_agent_id"])
    proposal = await proposal_service.apply_proposal(db, inp["proposal_id"], data)
    debug_stream.emit("proposal_applied", proposal_id=proposal.id,
                      page_id=proposal.target_page_id, executor=inp["executor_agent_id"])
    return {"proposal": _proposal_out(proposal)}


async def _add_page_relation(inp: dict, db: AsyncSession) -> dict:
    data = RelationCreate(
        source_page_id=inp["source_page_id"],
        target_page_id=inp["target_page_id"],
        relation_type=inp["relation_type"],
        created_by_agent=inp["created_by_agent"],
    )
    rel = await page_service.add_relation(db, data)
    return {
        "relation": {
            "id": rel.id,
            "source_page_id": rel.source_page_id,
            "target_page_id": rel.target_page_id,
            "relation_type": rel.relation_type,
        }
    }


async def _create_agent_task(inp: dict, db: AsyncSession) -> dict:
    ctx = inp.get("context_json")
    if isinstance(ctx, dict):
        ctx = json.dumps(ctx, ensure_ascii=False)
    task = AgentTask(
        agent_type=inp["agent_type"],
        instruction=inp["instruction"],
        context_json=ctx,
        batch_id=inp.get("batch_id"),
        status=TaskStatus.pending,
    )
    db.add(task)
    await db.flush()
    debug_stream.emit("task_created", task_id=task.id, agent_type=task.agent_type,
                      instruction=task.instruction[:100], batch_id=task.batch_id)
    return {"task": _task_out(task)}


async def _list_agent_tasks(inp: dict, db: AsyncSession) -> dict:
    from sqlalchemy import select
    stmt = select(AgentTask)
    if inp.get("status"):
        stmt = stmt.where(AgentTask.status == inp["status"])
    if inp.get("agent_type"):
        stmt = stmt.where(AgentTask.agent_type == inp["agent_type"])
    if inp.get("batch_id"):
        stmt = stmt.where(AgentTask.batch_id == inp["batch_id"])
    stmt = stmt.order_by(AgentTask.created_at.asc()).limit(min(int(inp.get("limit", 20)), 100))
    result = await db.scalars(stmt)
    tasks = list(result.all())
    return {"tasks": [_task_out(t) for t in tasks]}


async def _complete_agent_task(inp: dict, db: AsyncSession) -> dict:
    from sqlalchemy import select
    task = await db.scalar(select(AgentTask).where(AgentTask.id == inp["task_id"]))
    if task is None:
        return {"error": f"Task '{inp['task_id']}' not found."}
    outcome = inp["outcome"]
    task.status = TaskStatus.done if outcome == "done" else TaskStatus.failed
    task.completed_at = datetime.now(timezone.utc)
    if outcome == "failed":
        task.error_message = inp.get("error_message", "")
    await db.flush()
    debug_stream.emit("task_updated", task_id=task.id, agent_type=task.agent_type,
                      status=task.status, outcome=outcome)
    return {"task": _task_out(task)}


def _resolve_allowed_path(raw_path: str, allowed_dirs: list[pathlib.Path]) -> pathlib.Path | None:
    """Resolve a requested path and verify it falls inside an allowed directory.

    Returns the resolved absolute Path, or None if access is denied.
    """
    requested = pathlib.Path(raw_path)
    if requested.is_absolute():
        resolved = requested.resolve()
    else:
        # Try CWD-relative first, then each allowed dir as base
        resolved = None
        candidates = [pathlib.Path.cwd() / requested] + [d / requested for d in allowed_dirs]
        for candidate in candidates:
            try:
                r = candidate.resolve()
                if r.exists():
                    resolved = r
                    break
            except OSError:
                continue
        if resolved is None:
            resolved = (pathlib.Path.cwd() / requested).resolve()

    is_allowed = any(resolved == d or d in resolved.parents for d in allowed_dirs)
    return resolved if is_allowed else None


async def _read_file(inp: dict, db: AsyncSession) -> dict:
    import re

    # Parse allowed directories from settings
    raw_dirs = settings.file_read_allowed_dirs.strip()
    if not raw_dirs:
        return {"error": "File read tool is disabled. Set FILE_READ_ALLOWED_DIRS in .env to enable it."}

    allowed_dirs = [
        pathlib.Path(d.strip()).resolve()
        for d in raw_dirs.split(",")
        if d.strip()
    ]
    if not allowed_dirs:
        return {"error": "No valid allowed directories configured."}

    resolved = _resolve_allowed_path(inp["path"], allowed_dirs)
    if resolved is None:
        allowed_display = ", ".join(str(d) for d in allowed_dirs)
        return {"error": f"Access denied. Path is not within any allowed directory: {allowed_display}"}

    if not resolved.exists():
        return {"error": f"File not found: {resolved}"}
    if not resolved.is_file():
        return {"error": f"Path is not a file: {resolved}"}

    encoding = inp.get("encoding", "utf-8")
    try:
        all_lines = resolved.read_text(encoding=encoding).splitlines()
    except UnicodeDecodeError:
        return {"error": f"Cannot decode file as {encoding}. It may be a binary file or use a different encoding."}

    total_lines = len(all_lines)
    size = resolved.stat().st_size

    # ── Search mode ───────────────────────────────────────────────────────────
    search_pattern = inp.get("search_pattern")
    if search_pattern:
        context_lines = max(0, int(inp.get("context_lines", 2)))
        try:
            pattern = re.compile(search_pattern, re.IGNORECASE)
        except re.error as exc:
            return {"error": f"Invalid regex pattern: {exc}"}

        matches = []
        included: set[int] = set()
        for i, line in enumerate(all_lines):
            if pattern.search(line):
                start = max(0, i - context_lines)
                end = min(total_lines - 1, i + context_lines)
                for j in range(start, end + 1):
                    if j not in included:
                        included.add(j)
                        matches.append({
                            "line_number": j + 1,  # 1-based for human readability
                            "text": all_lines[j],
                            "is_match": j == i,
                        })

        debug_stream.emit("file_search", path=str(resolved), pattern=search_pattern, matches=len(matches))
        return {
            "path": str(resolved),
            "total_lines": total_lines,
            "search_pattern": search_pattern,
            "match_count": sum(1 for m in matches if m["is_match"]),
            "results": matches,
        }

    # ── Paginated read mode ───────────────────────────────────────────────────
    offset = max(0, int(inp.get("offset_lines", 0)))
    max_lines = min(max(1, int(inp.get("max_lines", 200))), 1000)

    page_lines = all_lines[offset: offset + max_lines]
    has_more = (offset + max_lines) < total_lines

    # Byte-size guard on the returned chunk (not the whole file)
    chunk = "\n".join(page_lines)
    if len(chunk.encode(encoding, errors="replace")) > settings.file_read_max_bytes:
        # Truncate to byte limit
        chunk = chunk.encode(encoding, errors="replace")[: settings.file_read_max_bytes].decode(encoding, errors="replace")
        has_more = True

    debug_stream.emit("file_read", path=str(resolved), offset=offset, lines=len(page_lines), size=size)
    return {
        "path": str(resolved),
        "total_lines": total_lines,
        "offset_lines": offset,
        "returned_lines": len(page_lines),
        "has_more": has_more,
        "content": chunk,
    }


async def _list_ingest_records(inp: dict, db: AsyncSession) -> dict:
    from sqlalchemy import select
    from core.models.ingest import FileIngestRecord

    stmt = select(FileIngestRecord)
    if inp.get("status"):
        stmt = stmt.where(FileIngestRecord.status == inp["status"])
    if inp.get("path_contains"):
        stmt = stmt.where(FileIngestRecord.path.contains(inp["path_contains"]))
    stmt = stmt.order_by(FileIngestRecord.last_scanned_at.desc()).limit(
        min(int(inp.get("limit", 50)), 200)
    )
    result = await db.scalars(stmt)
    records = list(result.all())
    return {
        "records": [
            {
                "id": r.id,
                "path": r.path,
                "status": r.status,
                "summary": r.summary,
                "related_page_ids": r.related_page_ids,
                "content_hash": r.content_hash,
                "last_scanned_at": r.last_scanned_at.isoformat() if r.last_scanned_at else None,
                "last_processed_at": r.last_processed_at.isoformat() if r.last_processed_at else None,
                "error_message": r.error_message,
            }
            for r in records
        ]
    }


async def _complete_file_ingest(inp: dict, db: AsyncSession) -> dict:
    import json as _json
    from sqlalchemy import select
    from core.models.ingest import FileIngestRecord, IngestStatus

    record = await db.scalar(
        select(FileIngestRecord).where(FileIngestRecord.id == inp["record_id"])
    )
    if record is None:
        return {"error": f"FileIngestRecord '{inp['record_id']}' not found."}

    outcome = inp["outcome"]
    record.status = IngestStatus.done if outcome == "done" else IngestStatus.failed
    record.last_processed_at = datetime.now(timezone.utc)

    if inp.get("summary"):
        record.summary = inp["summary"]
    if inp.get("related_page_ids"):
        record.related_page_ids = _json.dumps(inp["related_page_ids"])
    if outcome == "failed":
        record.error_message = inp.get("error_message", "")

    await db.flush()
    debug_stream.emit(
        "file_ingest_complete",
        record_id=record.id,
        path=record.path,
        outcome=outcome,
    )
    return {
        "record": {
            "id": record.id,
            "path": record.path,
            "status": record.status,
            "summary": record.summary,
        }
    }


async def _spawn_subagents(inp: dict, db: AsyncSession) -> dict:
    import asyncio

    from core.db.base import AsyncSessionLocal
    from core.services.llm_service import run_tool_loop
    from core.tools.definitions import TOOL_MAP

    _SUBAGENT_TOOLS = [
        "search_pages", "get_page", "list_pages",
        "get_related_pages", "get_page_history",
        "read_file", "list_files",
    ]
    _SUBAGENT_TOOL_DEFS = [TOOL_MAP[n] for n in _SUBAGENT_TOOLS if n in TOOL_MAP]

    _SUBAGENT_SYSTEM_PROMPT = """\
You are a focused read-only research subagent. You have been given a single task.
Use your available tools to gather the requested information, then produce a clear,
structured answer. Do not attempt any write operations — you have no write tools.
Complete your task efficiently and stop when done.
"""

    tasks = inp.get("tasks", [])
    if not tasks:
        return {"error": "No tasks provided."}
    if len(tasks) > 2:
        tasks = tasks[:2]  # Hard cap at 2

    async def _run_one(task: dict) -> dict:
        task_id = task.get("task_id", "task")
        instruction = task.get("instruction", "")
        context = task.get("context", "")
        user_message = instruction
        if context:
            user_message = f"{instruction}\n\nAdditional context:\n{context}"

        debug_stream.emit("subagent_start", task_id=task_id)
        try:
            async with AsyncSessionLocal() as sub_db:
                result = await run_tool_loop(
                    agent_type="subagent",
                    system_prompt=_SUBAGENT_SYSTEM_PROMPT,
                    user_message=user_message,
                    tool_definitions=_SUBAGENT_TOOL_DEFS,
                    db=sub_db,
                    max_iterations=10,
                )
            debug_stream.emit(
                "subagent_done",
                task_id=task_id,
                result_preview=(result[:120] + "…") if len(result) > 120 else result,
            )
            return {"task_id": task_id, "status": "done", "result": result}
        except Exception as exc:
            logger.warning("Subagent task %s failed: %s", task_id, exc)
            debug_stream.emit("subagent_error", task_id=task_id, error=str(exc))
            return {"task_id": task_id, "status": "failed", "error": str(exc)}

    results = await asyncio.gather(*[_run_one(t) for t in tasks])
    return {"subagent_results": list(results)}


async def _list_files(inp: dict, db: AsyncSession) -> dict:
    raw_dirs = settings.file_read_allowed_dirs.strip()
    if not raw_dirs:
        return {"error": "File read tool is disabled. Set FILE_READ_ALLOWED_DIRS in .env to enable it."}

    allowed_dirs = [
        pathlib.Path(d.strip()).resolve()
        for d in raw_dirs.split(",")
        if d.strip()
    ]
    if not allowed_dirs:
        return {"error": "No valid allowed directories configured."}

    # Normalise pattern for rglob: strip leading "**/" so callers can pass either
    # "*.md" or "**/*.md" and both recurse into subdirectories correctly.
    raw_pattern = inp.get("pattern") or ""
    import re as _re
    rglob_pattern = _re.sub(r"^\*\*/", "", raw_pattern) if raw_pattern else "*"
    limit = min(max(1, int(inp.get("limit", 50))), 200)

    # Determine search roots
    base_dir_raw = inp.get("base_dir")
    if base_dir_raw:
        search_root = _resolve_allowed_path(base_dir_raw, allowed_dirs)
        if search_root is None:
            allowed_display = ", ".join(str(d) for d in allowed_dirs)
            return {"error": f"Access denied. base_dir is not within any allowed directory: {allowed_display}"}
        if not search_root.is_dir():
            return {"error": f"base_dir is not a directory: {search_root}"}
        search_roots = [search_root]
    else:
        search_roots = allowed_dirs

    found: list[dict] = []
    seen: set[pathlib.Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        try:
            # Iterate lazily so the limit break fires before rglob exhausts the dir.
            # Do NOT wrap in sorted() — that forces the entire generator into memory first.
            for p in root.rglob(rglob_pattern):
                if p in seen:
                    continue
                seen.add(p)
                if p.is_file():
                    found.append({
                        "path": str(p),
                        "relative_path": str(p.relative_to(root)),
                        "size": p.stat().st_size,
                        "base_dir": str(root),
                    })
                    if len(found) >= limit:
                        break
        except Exception as exc:
            logger.warning("list_files glob error in %s: %s", root, exc)
        if len(found) >= limit:
            break

    debug_stream.emit("file_list", pattern=rglob_pattern, count=len(found))
    return {
        "pattern": rglob_pattern,
        "count": len(found),
        "truncated": len(found) >= limit,
        "files": found,
    }


async def _quick_query(inp: dict, db: AsyncSession) -> dict:
    """Drive a focused query agent and return its summarised answer."""
    from core.db.base import AsyncSessionLocal
    from core.services.llm_service import run_tool_loop
    from core.tools.definitions import TOOL_MAP

    _ALL_QUERY_TOOLS = [
        "search_pages", "get_page", "list_pages",
        "get_related_pages", "get_page_history",
        "read_file", "list_files",
    ]

    query = inp.get("query", "").strip()
    if not query:
        return {"error": "query is required."}

    summary_instruction = inp.get("summary_instruction", "").strip()
    max_words = min(max(10, int(inp.get("max_words", 150))), 800)

    # Validate and filter the allowed_tools list if provided
    requested_tools = inp.get("allowed_tools")
    if requested_tools:
        tool_names = [t for t in requested_tools if t in _ALL_QUERY_TOOLS]
        if not tool_names:
            return {"error": "allowed_tools contains no valid query tool names."}
    else:
        tool_names = _ALL_QUERY_TOOLS

    tool_defs = [TOOL_MAP[n] for n in tool_names if n in TOOL_MAP]

    summary_directive = (
        summary_instruction
        if summary_instruction
        else "Produce a concise, neutral summary of the findings."
    )

    system_prompt = (
        "You are a focused read-only query agent. You have been given a single query.\n"
        "Use your available tools to gather the relevant information, then produce a summary.\n\n"
        f"Summary instruction: {summary_directive}\n"
        f"Word limit: {max_words} words maximum for the summary.\n\n"
        "At the end of your response, add a '## Sources' section listing every page slug "
        "or file path you consulted, one per line. Do not perform write operations."
    )
    user_message = query

    debug_stream.emit("quick_query_start", query=query[:120], max_words=max_words)
    try:
        async with AsyncSessionLocal() as query_db:
            raw_result = await run_tool_loop(
                agent_type="quick_query",
                system_prompt=system_prompt,
                user_message=user_message,
                tool_definitions=tool_defs,
                db=query_db,
                max_iterations=12,
            )
    except Exception as exc:
        logger.warning("quick_query failed: %s", exc)
        debug_stream.emit("quick_query_error", error=str(exc))
        return {"error": str(exc)}

    # Split off the Sources section if the agent included one
    summary = raw_result
    sources: list[str] = []
    if "## Sources" in raw_result:
        parts = raw_result.split("## Sources", 1)
        summary = parts[0].strip()
        sources = [line.strip("- \t") for line in parts[1].strip().splitlines() if line.strip()]

    debug_stream.emit(
        "quick_query_done",
        query=query[:120],
        summary_preview=(summary[:100] + "…") if len(summary) > 100 else summary,
        sources=sources,
    )
    return {"summary": summary, "sources": sources}


def _resolve_output_md_path(raw_path: str) -> pathlib.Path | None:
    """Resolve a path under ./output and enforce `.md` extension."""
    base_dir = (pathlib.Path.cwd() / "output").resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    requested = pathlib.Path(raw_path.strip())
    if not requested.name:
        return None

    if requested.is_absolute():
        # Absolute paths are allowed only if they are still inside ./output.
        resolved = requested.resolve()
    else:
        resolved = (base_dir / requested).resolve()

    if not (resolved == base_dir or base_dir in resolved.parents):
        return None
    if resolved.suffix.lower() != ".md":
        return None
    return resolved


def _upsert_section(content: str, section: str, payload: str, *, append: bool) -> str:
    """Replace or append content inside a named fenced section marker."""
    start_marker = f"<!-- BEGIN:{section} -->"
    end_marker = f"<!-- END:{section} -->"
    start = content.find(start_marker)
    if start == -1:
        block = f"{start_marker}\n{payload.rstrip()}\n{end_marker}\n"
        if not content.strip():
            return block
        return content.rstrip() + "\n\n" + block

    end = content.find(end_marker, start + len(start_marker))
    if end == -1:
        # Broken section markers: rewrite from start marker to EOF.
        prefix = content[:start]
        block = f"{start_marker}\n{payload.rstrip()}\n{end_marker}\n"
        return prefix.rstrip() + "\n\n" + block if prefix.strip() else block

    body_start = start + len(start_marker)
    old_body = content[body_start:end]
    if append:
        merged_body = old_body.rstrip("\n") + ("\n" if old_body.strip() else "") + payload.rstrip() + "\n"
    else:
        merged_body = "\n" + payload.rstrip() + "\n"
    return content[:body_start] + merged_body + content[end:]


async def _output_md_write(inp: dict, db: AsyncSession) -> dict:
    path_value = inp.get("path", "")
    resolved = _resolve_output_md_path(path_value)
    if resolved is None:
        return {"error": "Invalid path. Must be a .md file inside ./output."}

    mode = inp.get("mode", "overwrite")
    content = inp.get("content", "")
    section = (inp.get("section") or "").strip()

    if not isinstance(content, str):
        return {"error": "content must be a string."}
    if mode not in {"overwrite", "append", "replace_section", "append_section"}:
        return {"error": f"Unknown mode: {mode}"}
    if mode in {"replace_section", "append_section"} and not section:
        return {"error": "section is required for section-based modes."}

    resolved.parent.mkdir(parents=True, exist_ok=True)
    old_content = resolved.read_text(encoding="utf-8") if resolved.exists() else ""

    if mode == "overwrite":
        new_content = content
    elif mode == "append":
        if old_content and not old_content.endswith("\n"):
            old_content += "\n"
        new_content = old_content + content
    elif mode == "replace_section":
        new_content = _upsert_section(old_content, section, content, append=False)
    else:  # append_section
        new_content = _upsert_section(old_content, section, content, append=True)

    resolved.write_text(new_content, encoding="utf-8")
    debug_stream.emit(
        "output_md_write",
        path=str(resolved),
        mode=mode,
        section=section or None,
        bytes=len(new_content.encode("utf-8")),
    )
    return {
        "path": str(resolved),
        "mode": mode,
        "section": section or None,
        "bytes_written": len(new_content.encode("utf-8")),
        "char_count": len(new_content),
    }


async def _output_md_copy_page(inp: dict, db: AsyncSession) -> dict:
    path_value = inp.get("path", "")
    resolved = _resolve_output_md_path(path_value)
    if resolved is None:
        return {"error": "Invalid path. Must be a .md file inside ./output."}

    if inp.get("page_id"):
        page = await page_service.get_page(db, inp["page_id"])
    elif inp.get("slug"):
        page = await page_service.get_page_by_slug(db, inp["slug"])
    else:
        return {"error": "Provide either page_id or slug."}

    include_fields = inp.get("include_fields") or ["title", "slug", "summary", "content"]
    allowed = {"title", "slug", "summary", "content"}
    fields = [f for f in include_fields if f in allowed]
    if not fields:
        return {"error": "include_fields has no valid values."}

    lines: list[str] = []
    if "title" in fields:
        lines.append(f"# {page.title}")
    if "slug" in fields:
        lines.append(f"- slug: `{page.slug}`")
    if "summary" in fields:
        lines.append("\n## Summary\n")
        lines.append(page.summary or "")
    if "content" in fields:
        lines.append("\n## Content\n")
        lines.append(page.content or "")
    payload = "\n".join(lines).strip() + "\n"

    write_inp = {
        "path": path_value,
        "mode": inp.get("mode", "append"),
        "section": inp.get("section"),
        "content": payload,
    }
    result = await _output_md_write(write_inp, db)
    if "error" in result:
        return result
    result["copied_from"] = {"page_id": page.id, "slug": page.slug}
    return result


async def _output_md_copy_task(inp: dict, db: AsyncSession) -> dict:
    from sqlalchemy import select

    path_value = inp.get("path", "")
    resolved = _resolve_output_md_path(path_value)
    if resolved is None:
        return {"error": "Invalid path. Must be a .md file inside ./output."}

    task = await db.scalar(select(AgentTask).where(AgentTask.id == inp.get("task_id", "")))
    if task is None:
        return {"error": f"Task '{inp.get('task_id')}' not found."}

    lines = [
        f"## Task {task.id}",
        f"- agent_type: `{task.agent_type}`",
        f"- status: `{task.status}`",
        "",
        "### Instruction",
        task.instruction or "",
    ]
    if task.context_json:
        lines.extend(["", "### Context JSON", "```json", task.context_json, "```"])
    if task.error_message:
        lines.extend(["", "### Error", task.error_message])
    payload = "\n".join(lines).strip() + "\n"

    write_inp = {
        "path": path_value,
        "mode": inp.get("mode", "append"),
        "section": inp.get("section"),
        "content": payload,
    }
    result = await _output_md_write(write_inp, db)
    if "error" in result:
        return result
    result["copied_from"] = {"task_id": task.id, "agent_type": task.agent_type, "status": task.status}
    return result


async def _output_md_list(inp: dict, db: AsyncSession) -> dict:
    base_dir = (pathlib.Path.cwd() / "output").resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    limit = min(max(1, int(inp.get("limit", 50))), 200)

    files: list[dict] = []
    for p in base_dir.rglob("*.md"):
        if not p.is_file():
            continue
        files.append(
            {
                "path": str(p),
                "relative_path": str(p.relative_to(base_dir)),
                "size": p.stat().st_size,
                "updated_at": datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
        if len(files) >= limit:
            break

    return {"base_dir": str(base_dir), "count": len(files), "files": files}


async def _trigger_pipeline(inp: dict, db: AsyncSession) -> dict:
    import asyncio
    from core.db.base import AsyncSessionLocal

    pipeline_type = inp.get("pipeline_type", "maintenance")

    async def _run_maintenance() -> None:
        from core.services.agent_service import run_maintenance_pipeline
        async with AsyncSessionLocal() as _db:
            try:
                await run_maintenance_pipeline(_db)
            except Exception:
                logger.exception("Background maintenance pipeline error (director-triggered)")

    async def _run_ingest(scan: bool = False) -> None:
        from core.services.agent_service import run_ingest_pipeline
        async with AsyncSessionLocal() as _db:
            try:
                await run_ingest_pipeline(_db, scan_unprocessed=scan)
            except Exception:
                logger.exception("Background ingest pipeline error (director-triggered)")

    if pipeline_type == "maintenance":
        asyncio.ensure_future(_run_maintenance())
        message = "Maintenance pipeline queued."
    elif pipeline_type == "ingest":
        asyncio.ensure_future(_run_ingest(scan=False))
        message = "Ingest pipeline queued (new/changed/failed files)."
    elif pipeline_type == "ingest_scan":
        asyncio.ensure_future(_run_ingest(scan=True))
        message = "Ingest scan pipeline queued (all unprocessed files)."
    else:
        return {"error": f"Unknown pipeline_type: {pipeline_type}"}

    debug_stream.emit("pipeline_triggered", pipeline_type=pipeline_type, source="director")
    return {"status": "queued", "pipeline_type": pipeline_type, "message": message}


# ── dispatch table ────────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "search_pages": _search_pages,
    "get_page": _get_page,
    "list_pages": _list_pages,
    "get_related_pages": _get_related_pages,
    "compare_pages": _compare_pages,
    "get_page_history": _get_page_history,
    "list_proposals": _list_proposals,
    "propose_new_page": _propose_new_page,
    "propose_page_edit": _propose_page_edit,
    "review_proposal": _review_proposal,
    "apply_proposal": _apply_proposal,
    "add_page_relation": _add_page_relation,
    "create_agent_task": _create_agent_task,
    "list_agent_tasks": _list_agent_tasks,
    "complete_agent_task": _complete_agent_task,
    "read_file": _read_file,
    "list_files": _list_files,
    "list_ingest_records": _list_ingest_records,
    "complete_file_ingest": _complete_file_ingest,
    "spawn_subagents": _spawn_subagents,
    "quick_query": _quick_query,
    "trigger_pipeline": _trigger_pipeline,
    "output_md_write": _output_md_write,
    "output_md_copy_page": _output_md_copy_page,
    "output_md_copy_task": _output_md_copy_task,
    "output_md_list": _output_md_list,
    # manage_todo is handled directly in director_service, not via dispatch_tool
}


async def dispatch_tool(tool_name: str, tool_input: dict, db: AsyncSession) -> dict:
    """Execute a tool by name and return its result dict.

    Any service-level exception is caught and returned as {"error": "..."} so
    the LLM can handle it gracefully without crashing the tool loop.
    """
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        return await handler(tool_input, db)
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.warning("Tool %s raised %s", tool_name, error_msg)
        debug_stream.emit("tool_error", tool=tool_name, error=error_msg)
        return {"error": error_msg}
