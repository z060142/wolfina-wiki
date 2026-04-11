"""Agent orchestration layer.

Six specialist agents + one orchestrator, each with a distinct system prompt
and a curated tool subset.  All LLM interaction goes through run_tool_loop()
in llm_service — no agent ever calls the DB directly.

Conversation flush pipeline (called by conversation_service):
    run_flush_pipeline(conversation_text, batch_id, db)
        1. proposer  → searches wiki, creates proposals
        2. reviewer  → reviews pending proposals in this batch
        3. executor  → applies approved proposals
        4. relation  → links related pages

Scheduler maintenance pipeline (called by scheduler_service):
    run_maintenance_pipeline(db)
        1. orchestrator → reads wiki state, creates AgentTasks
        2. Each specialist agent → reads its tasks, executes them
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

# ── Pipeline priority coordination ───────────────────────────────────────────
# Flush pipelines (real-time conversation ingestion) take priority over the
# background maintenance pipeline.  Maintenance yields between stages when any
# flush is active or waiting.
_flush_waiters: int = 0          # number of flush pipelines active or waiting
_flush_done = asyncio.Event()    # fired each time a flush finishes
_flush_done.set()                # starts in "no flush running" state


async def _wait_for_flush_gap() -> None:
    """Block until no flush pipeline is active. Used by maintenance stages."""
    while _flush_waiters > 0:
        _flush_done.clear()
        await _flush_done.wait()

from core.debug.event_stream import debug_stream
from core.services.llm_service import run_tool_loop
from core.services.prompt_blocks import (
    BLOCK_AGENT_TASK_WORKFLOW,
    BLOCK_FILE_READ_PAGINATION,
    BLOCK_IDEMPOTENCY,
    BLOCK_NO_DUPLICATE_TASKS,
    BLOCK_PROPOSAL_GUIDELINES,
    BLOCK_RELATION_TYPES,
    BLOCK_ROLE_SEPARATION,
    BLOCK_SPAWN_SUBAGENTS,
    build_prompt,
)
from core.settings import settings
from core.tools.definitions import get_tools_for_agent

logger = logging.getLogger(__name__)


# ── system prompts ────────────────────────────────────────────────────────────

_PROPOSER_PROMPT = build_prompt(
    role=(
        "You are the Proposer agent for the Wolfina Wiki system.\n"
        "Your job is to analyse a conversation and extract knowledge worth storing in the wiki."
    ),
    sections=[
        """\
== FLUSH MODE (you receive a conversation transcript) ==
1. Use search_pages to check if a relevant page already exists.
2. If it exists, use propose_page_edit to update it with new information.
3. If it does not exist, use propose_new_page to create it.
4. Use the provided batch_id on every proposal for traceability.

Only extract information that meets ALL of the following:
- Explicitly stated (not inferred or assumed from tone)
- Useful to know in a future conversation — preferences, identity, goals, ongoing projects,
  opinions, or facts about a topic the user cares about
- Not purely transient — a momentary reaction with no lasting meaning does not qualify

Key distinction: the casual TONE of a message does not disqualify it.
A relaxed, chatty sentence can still contain a recordable fact or preference.
Ask yourself: "If this appeared in a summary before the next conversation, would it help
me understand this person or topic better?" If yes, record it.

Do NOT record: pure social filler ("haha", "ok", "got it"), rhetorical questions,
content the user explicitly marked as hypothetical, or negated claims.""",
        """\
== MAINTENANCE MODE (you receive agent tasks via list_agent_tasks) ==
1. Use list_agent_tasks to find your pending tasks.
2. For each task, check context_json for the action type:
   - "revise_and_resubmit": A previous proposal was rejected. Read the rejection_reason in
     context_json carefully. Then decide:
     (a) If the rejection reason is fixable (e.g. missing detail, wrong format, conflicting facts
         that can be resolved), use get_page to read current content and submit a corrected proposal
         that directly addresses the feedback. Do NOT resubmit the same content.
     (b) If the rejection reason reveals the proposal was fundamentally wrong, redundant, or the
         information no longer applies, call complete_agent_task with outcome="failed" and
         error_message explaining why you chose to abandon rather than revise. Do not resubmit.
   - (other tasks): Execute as instructed using search_pages, get_page, propose_new_page, propose_page_edit.
3. Use compare_pages when asked to merge pages — compare them before writing the merged content.
4. Call complete_agent_task with outcome="done" or "failed" when finished.""",
    ],
    blocks=[
        BLOCK_PROPOSAL_GUIDELINES,
        BLOCK_ROLE_SEPARATION,
        BLOCK_IDEMPOTENCY,
        BLOCK_AGENT_TASK_WORKFLOW,
        BLOCK_SPAWN_SUBAGENTS,
    ],
)

_REVIEWER_PROMPT = build_prompt(
    role=(
        "You are the Reviewer agent for the Wolfina Wiki system.\n"
        "Your job is to review pending edit proposals and approve or reject them."
    ),
    sections=[
        """\
== FLUSH MODE (reviewing proposals from a specific batch) ==
1. Use list_proposals with the provided batch_id to find pending proposals.
2. Use get_page to read the current page content (for edits).
3. Use get_page_history to check recent changes.
4. Use compare_pages when a proposal seems to duplicate an existing page — compare them before deciding.
5. Use review_proposal with decision="approve" or "reject" and a specific feedback message.""",
        """\
== MAINTENANCE MODE (you receive agent tasks via list_agent_tasks) ==
1. Use list_agent_tasks to find your pending tasks.
2. Also use list_proposals with status="pending" (no batch_id) to find any unreviewed proposals.
3. Review proposals using get_page, get_page_history, compare_pages, then review_proposal.
4. Call complete_agent_task with outcome="done" or "failed" when finished.""",
        """\
== REVIEW DECISION CRITERIA ==
Approve a proposal only if ALL of the following checks pass:
□ Content is factually accurate and supported by the rationale.
□ No direct contradiction with the existing page or related pages.
□ Markdown is well-formed — headings, lists, and code blocks are syntactically correct.
□ Slug (if present) is ASCII-lowercase-hyphenated: ^[a-z0-9]+(?:-[a-z0-9]+)*$
□ Content is substantive — not a stub (a page with only a title and one or two sentences).
□ Important relations are present (use get_related_pages to verify if uncertain).

Reject if any check fails. In your feedback, name the specific check(s) that failed.""",
        """\
== AFTER REJECTION: CLOSE THE FEEDBACK LOOP ==
When you reject a proposal, determine whether a revision attempt is still viable:

1. Call list_agent_tasks to count how many revise_and_resubmit tasks already exist for this
   proposal (match "original_proposal_id" in context_json).
2. If 2 or more prior revise tasks already exist for the same proposal, do NOT create another.
   The proposal has exhausted its revision attempts. Mark your reviewer task as complete with
   outcome="done" and note in your response that the proposal was permanently rejected.
3. Otherwise, call create_agent_task immediately after rejecting, with agent_type="proposer":
   - instruction: A clear description of what needs to be fixed, referencing the original proposal.
   - context_json: {
       "action": "revise_and_resubmit",
       "original_proposal_id": "<the rejected proposal id>",
       "target_page_id": "<page id if editing existing page, or null for new page>",
       "proposed_title": "<original proposed title>",
       "rejection_reason": "<your specific rejection feedback — be concrete>",
       "retry_count": <number of prior revise tasks found in step 1>
     }
This ensures your review decision produces actionable improvement, not an infinite revision loop.""",
    ],
    blocks=[
        BLOCK_ROLE_SEPARATION,
        BLOCK_AGENT_TASK_WORKFLOW,
    ],
)

_EXECUTOR_PROMPT = build_prompt(
    role=(
        "You are the Executor agent for the Wolfina Wiki system.\n"
        "Your job is to apply approved proposals to the wiki."
    ),
    sections=[
        """\
== FLUSH MODE (applying proposals from a specific batch) ==
1. Use list_proposals with status="approved" and the provided batch_id.
2. For each approved proposal, use apply_proposal with your executor_agent_id.""",
        """\
== MAINTENANCE MODE (you receive agent tasks via list_agent_tasks) ==
1. Use list_agent_tasks to find your pending tasks.
2. Also use list_proposals with status="approved" (no batch_id) to find all approved proposals.
3. Apply each using apply_proposal with your executor_agent_id.
4. Call complete_agent_task with outcome="done" or "failed" when finished.""",
        """\
== EXECUTION RULES ==
- Do not modify proposal content — apply them exactly as approved.
- Only apply proposals with status="approved"; skip all others.""",
    ],
    blocks=[
        BLOCK_ROLE_SEPARATION,
        BLOCK_AGENT_TASK_WORKFLOW,
    ],
)

_RELATION_PROMPT = build_prompt(
    role=(
        "You are the Relation agent for the Wolfina Wiki system.\n"
        "Your job is to enrich the wiki's knowledge graph by adding relations between pages."
    ),
    sections=[
        """\
== FLUSH MODE (linking pages from a specific batch) ==
1. Use list_pages or search_pages to find pages created or updated in this batch.
2. Use get_related_pages to check existing relations BEFORE adding new ones.
3. Use add_page_relation to add appropriate links only if they don't already exist.""",
        """\
== MAINTENANCE MODE (you receive agent tasks via list_agent_tasks) ==
1. Use list_agent_tasks to find your pending tasks.
2. Execute each task as instructed (use get_page, search_pages, get_related_pages, add_page_relation).
3. Call complete_agent_task with outcome="done" or "failed" when finished.""",
        """\
== TERMINATION RULE ==
Stop when all relevant relations have been attempted. Do not repeat the same calls.""",
    ],
    blocks=[
        BLOCK_RELATION_TYPES,
        BLOCK_AGENT_TASK_WORKFLOW,
    ],
)

_RESEARCH_PROMPT = build_prompt(
    role=(
        "You are the Research agent for the Wolfina Wiki system.\n"
        "Your job is to gather and summarise information from the wiki in response to a specific task."
    ),
    sections=[
        """\
== STEPS ==
1. Use list_agent_tasks to find your pending tasks.
2. For each task, use search_pages, get_page, list_pages, get_related_pages, and get_page_history
   to collect relevant information.
3. Produce a clear, structured summary of what you found.
4. Call complete_agent_task with outcome="done" or "failed" when finished.""",
    ],
    blocks=[
        BLOCK_AGENT_TASK_WORKFLOW,
        BLOCK_SPAWN_SUBAGENTS,
    ],
)

_ORCHESTRATOR_PROMPT = build_prompt(
    role=(
        "You are the Orchestrator agent for the Wolfina Wiki system.\n"
        "Your job is to evaluate the current state of the wiki and create a prioritised work queue\n"
        "for specialist agents (research, proposer, reviewer, executor, relation, ingest)."
    ),
    sections=[
        """\
== MAINTENANCE TASKS ==
1. Use list_pages to review recently updated pages.
2. Use list_proposals to check for stale pending/approved proposals.
3. Use list_agent_tasks to see what is already queued.
4. Create maintenance tasks using create_agent_task as needed.

Example maintenance tasks:
- Ask research agent to summarise orphaned pages
- Ask proposer agent to merge near-duplicate pages
- Ask reviewer agent to review long-pending proposals
- Ask executor agent to apply all approved proposals
- Ask relation agent to link recently created pages""",
        """\
== DUPLICATE PAGE DETECTION ==
After reviewing recently updated pages, perform a duplicate check:

1. Review the page titles you already retrieved from list_pages. Identify pairs whose titles
   suggest the same topic (e.g. synonyms, singular/plural, same concept different wording).
   Do this reasoning yourself — do NOT delegate it to quick_query.

2. For each suspicious pair you identified, use compare_pages with their page_ids to
   inspect the actual content side-by-side.

3. If the pages are genuinely duplicate or highly overlapping, create a proposer task:
   - agent_type: "proposer"
   - instruction: "Merge duplicate pages: consolidate the content of '<title A>' into
     '<title B>', keeping all unique facts. Then propose to archive '<title A>'."
   - context_json: {
       "action": "merge_pages",
       "source_page_id": "<page A id — to be merged from>",
       "target_page_id": "<page B id — to be merged into>",
       "source_title": "<title A>",
       "target_title": "<title B>",
       "merge_reason": "<one sentence explaining why they are duplicates>"
     }

4. Only flag pairs where the content genuinely overlaps. Skip pairs that are merely
   related topics.""",
        """\
== INGEST PLANNING (Round 1 — per-file scanning) ==
When the mode includes ingest work:
1. Use list_files to discover all files in the allowed directories.
2. Use list_ingest_records to see known files and their hashes/summaries.
3. For files that are NEW (not in records) or CHANGED (hash mismatch — hash is in context_json):
   - Create an ingest task: agent_type="ingest", instruction describes the file to process.
   - Include {"path": "<absolute_path>", "record_id": "<id_if_existing>"} in context_json.
4. Do NOT create ingest tasks for files already status=done with an unchanged hash.""",
        """\
== INGEST PLANNING (Round 2 — cross-file synthesis) ==
When all pending ingest tasks are done:
1. Use list_ingest_records with status="done" to read all file summaries.
2. Use list_pages to see current wiki state.
3. Decide how to group files into wiki pages (many-to-many is fine).
4. Create proposer tasks with instructions referencing specific record IDs and target page strategy.
   Include relevant record_ids in context_json so the ingest agent can re-read summaries.""",
        """\
== TASK CREATION RULES ==
Be specific in every task instruction — include record IDs, page IDs, or file paths in context_json.

Before creating new tasks of any type, call list_agent_tasks and count pending tasks per agent_type.
If a given agent_type already has 3 or more pending tasks, do NOT add more tasks of that type —
the backlog is already sufficient. Note this in your reasoning and move on.""",
    ],
    blocks=[
        BLOCK_NO_DUPLICATE_TASKS,
        BLOCK_SPAWN_SUBAGENTS,
    ],
)

_INGEST_PROMPT = build_prompt(
    role=(
        "You are the Ingest agent for the Wolfina Wiki system.\n"
        "Your job is to read external files, extract and synthesise knowledge from them,\n"
        "and prepare structured content for the proposer agent to turn into wiki pages."
    ),
    sections=[
        """\
== ROUND 1 MODE (per-file processing) ==
You receive a task with a specific file path to process.

1. Use list_agent_tasks to find your pending task and extract the file path from context_json.
2. Use read_file to read the file. For large files, use pagination (offset_lines + max_lines)
   or search_pattern to locate relevant sections — do NOT try to read everything at once.
3. If the file references other already-processed files that are relevant, use list_ingest_records
   to find their summaries (status="done") — you can use these as background context without
   re-reading the raw files.
4. Produce a concise summary of what the file contains: key topics, facts, structure.
5. Call complete_file_ingest with:
   - record_id from context_json
   - outcome="done"
   - summary: your concise description of the file's content (this is what the orchestrator
     will use for cross-file planning — make it informative but compact)
   - related_page_ids: [] (leave empty in Round 1; populated by the proposer later)
6. Call complete_agent_task with outcome="done".""",
        """\
== ROUND 2 MODE (cross-file synthesis) ==
You receive a task asking you to synthesise content from multiple files into wiki page proposals.

1. Use list_agent_tasks to find your pending task. The context_json will list record_ids and
   a target page strategy.
2. Use list_ingest_records to read summaries for all relevant records.
3. Use read_file on the actual files as needed to get full detail.
4. Use search_pages / list_pages to check if target pages already exist.
5. Use create_agent_task to create proposer tasks with the synthesised content.
   Pass the full proposed content, title, rationale, and source file paths in context_json.
6. Call complete_agent_task with outcome="done".""",
        """\
== INGEST GUIDELINES ==
- A single file may contribute to multiple wiki pages; multiple files may merge into one page.
- Be faithful to the source material. Do not invent information.
- In the summary, capture WHAT the file is about (not HOW you read it).
- If a file is binary, encrypted, or unreadable, mark it failed with a clear error_message.""",
    ],
    blocks=[
        BLOCK_FILE_READ_PAGINATION,
        BLOCK_AGENT_TASK_WORKFLOW,
        BLOCK_SPAWN_SUBAGENTS,
    ],
)


# ── pipeline helpers ──────────────────────────────────────────────────────────

async def _run_agent(
    agent_type: str,
    user_message: str,
    db: AsyncSession,
    batch_id: str = "",
) -> str:
    debug_stream.emit("agent_start", agent_type=agent_type, batch_id=batch_id)
    result = await run_tool_loop(
        agent_type=agent_type,
        system_prompt=_SYSTEM_PROMPTS[agent_type],
        user_message=user_message,
        tool_definitions=get_tools_for_agent(agent_type),
        db=db,
    )
    debug_stream.emit("agent_done", agent_type=agent_type, batch_id=batch_id,
                      result_preview=(result[:120] + "…") if len(result) > 120 else result)
    return result


_SYSTEM_PROMPTS: dict[str, str] = {
    "proposer": _PROPOSER_PROMPT,
    "reviewer": _REVIEWER_PROMPT,
    "executor": _EXECUTOR_PROMPT,
    "relation": _RELATION_PROMPT,
    "research": _RESEARCH_PROMPT,
    "orchestrator": _ORCHESTRATOR_PROMPT,
    "ingest": _INGEST_PROMPT,
}


# ── public pipelines ──────────────────────────────────────────────────────────

async def run_flush_pipeline(
    conversation_text: str,
    batch_id: str,
    db: AsyncSession,
) -> None:
    """Process a flushed conversation window through the full agent pipeline.

    Runs: proposer → reviewer → executor → relation
    Each stage commits its work to the DB before the next stage begins.
    Flush pipelines take priority over the maintenance pipeline.
    """
    global _flush_waiters, _flush_done
    _flush_waiters += 1
    _flush_done.clear()
    logger.info("Flush pipeline start — batch_id=%s", batch_id)

    # 1. Proposer
    proposer_msg = (
        f"batch_id: {batch_id}\n"
        f"proposer_agent_id: {settings.proposer_agent_id}\n\n"
        f"--- CONVERSATION ---\n{conversation_text}"
    )
    try:
        await _run_agent("proposer", proposer_msg, db, batch_id=batch_id)
        await db.commit()
        logger.info("Flush pipeline: proposer done — batch_id=%s", batch_id)
    except Exception:
        logger.exception("Flush pipeline: proposer failed — batch_id=%s", batch_id)
        await db.rollback()
        return

    # 2. Reviewer
    reviewer_msg = (
        f"batch_id: {batch_id}\n"
        f"reviewer_agent_id: {settings.reviewer_agent_id}\n\n"
        f"Review all pending proposals in batch_id={batch_id}."
    )
    try:
        await _run_agent("reviewer", reviewer_msg, db, batch_id=batch_id)
        await db.commit()
        logger.info("Flush pipeline: reviewer done — batch_id=%s", batch_id)
    except Exception:
        logger.exception("Flush pipeline: reviewer failed — batch_id=%s", batch_id)
        await db.rollback()

    # 3. Executor
    executor_msg = (
        f"batch_id: {batch_id}\n"
        f"executor_agent_id: {settings.executor_agent_id}\n\n"
        f"Apply all approved proposals in batch_id={batch_id}."
    )
    try:
        await _run_agent("executor", executor_msg, db, batch_id=batch_id)
        await db.commit()
        logger.info("Flush pipeline: executor done — batch_id=%s", batch_id)
    except Exception:
        logger.exception("Flush pipeline: executor failed — batch_id=%s", batch_id)
        await db.rollback()

    # 4. Relation agent — pass applied page info directly so the agent doesn't need batch_id lookup
    from core.models.proposal import EditProposal, ProposalStatus
    from sqlalchemy import select as _select
    applied_rows = await db.scalars(
        _select(EditProposal).where(
            EditProposal.batch_id == batch_id,
            EditProposal.status == ProposalStatus.applied,
        )
    )
    applied_list = list(applied_rows.all())
    if applied_list:
        page_hints = "\n".join(
            f"  - title={p.proposed_title}"
            + (f"  slug={p.proposed_slug}" if p.proposed_slug else "")
            + (f"  page_id={p.target_page_id}" if p.target_page_id else "  (new page)")
            for p in applied_list
        )
        relation_msg = (
            f"batch_id: {batch_id}\n"
            f"relation_agent_id: {settings.relation_agent_id}\n\n"
            f"Add relations for pages created or updated in this batch.\n\n"
            f"Pages affected:\n{page_hints}\n\n"
            f"Use search_pages by title or slug to locate each page, then add appropriate relations."
        )
    else:
        relation_msg = (
            f"batch_id: {batch_id}\n"
            f"relation_agent_id: {settings.relation_agent_id}\n\n"
            "No proposals were applied in this batch. Nothing to do — stop immediately."
        )
    try:
        await _run_agent("relation", relation_msg, db, batch_id=batch_id)
        await db.commit()
        logger.info("Flush pipeline: relation done — batch_id=%s", batch_id)
    except Exception:
        logger.exception("Flush pipeline: relation failed — batch_id=%s", batch_id)
        await db.rollback()

    _flush_waiters -= 1
    if _flush_waiters == 0:
        _flush_done.set()
    logger.info("Flush pipeline complete — batch_id=%s", batch_id)


async def run_maintenance_pipeline(db: AsyncSession) -> None:
    """Scheduled maintenance: orchestrator creates tasks, then specialists execute them.

    Runs: orchestrator → research → ingest → proposer → reviewer → executor → relation
    Each specialist processes ALL pending tasks of its type (any source, any batch_id),
    including tasks created by the Director agent.
    """
    import uuid
    batch_id = str(uuid.uuid4())
    logger.info("Maintenance pipeline start — batch_id=%s", batch_id)

    # 1. Orchestrator builds the work queue
    # Yield to any waiting flush pipeline before starting.
    await _wait_for_flush_gap()
    orch_msg = (
        f"batch_id: {batch_id}\n\n"
        "Evaluate the current wiki state and create tasks for specialist agents as needed.\n"
        "IMPORTANT: Do NOT create ingest tasks — file ingestion is handled by a separate "
        "pipeline and must not be triggered from here."
    )
    try:
        await _run_agent("orchestrator", orch_msg, db, batch_id=batch_id)
        await db.commit()
    except Exception:
        logger.exception("Maintenance: orchestrator failed — batch_id=%s", batch_id)
        await db.rollback()
        return

    # 2. Run each specialist against its pending tasks in this batch
    # Note: reviewer and executor also handle any globally pending/approved proposals
    # (maintenance proposer proposals do not carry the maintenance batch_id).
    _extra: dict[str, str] = {
        "ingest": (
            "For each ingest task, read the file at the path given in context_json, "
            "produce a summary, then call complete_file_ingest (using the record_id from context_json) "
            "and complete_agent_task. If context_json has no record_id, use list_ingest_records "
            "to find the matching record by path, or skip and mark the task failed with a clear error."
        ),
        "reviewer": (
            "Also use list_proposals with status=\"pending\" (no batch_id filter) "
            "to find any proposals awaiting review, and review them."
        ),
        "executor": (
            "Also use list_proposals with status=\"approved\" (no batch_id filter) "
            "to find any approved proposals, and apply them."
        ),
    }
    from core.models.conversation import AgentTask, TaskStatus
    from sqlalchemy import select as _select, func as _func

    for agent_type in ("research", "ingest", "proposer", "reviewer", "executor", "relation"):
        # Yield between each specialist stage — flush pipelines take priority.
        await _wait_for_flush_gap()
        agent_id = getattr(settings, f"{agent_type}_agent_id")
        extra = _extra.get(agent_type, "")
        specialist_msg = (
            f"batch_id: {batch_id}\n"
            f"{agent_type}_agent_id: {agent_id}\n\n"
            f"Use list_agent_tasks with agent_type=\"{agent_type}\" and status=\"pending\" "
            f"to find ALL your pending tasks (do NOT filter by batch_id — process tasks from any source, "
            f"including tasks created by the Director or previous pipeline runs). "
            f"Execute each task using your tools (your agent id is \"{agent_id}\"). "
            + (f"{extra} " if extra else "")
            + "Call complete_agent_task with outcome=\"done\" or \"failed\" for each task when finished. "
            "If there are no tasks for you, do nothing."
        )
        # Ingest tasks can exceed max_iterations in a single pass — keep retrying
        # until no pending ingest tasks remain (up to a safety cap).
        _max_passes = 10 if agent_type == "ingest" else 1
        for _pass in range(_max_passes):
            try:
                await _run_agent(agent_type, specialist_msg, db, batch_id=batch_id)
                await db.commit()
                logger.info("Maintenance: %s pass %d done — batch_id=%s", agent_type, _pass + 1, batch_id)
            except Exception:
                logger.exception("Maintenance: %s pass %d failed — batch_id=%s", agent_type, _pass + 1, batch_id)
                await db.rollback()
                break
            if agent_type == "ingest":
                remaining = await db.scalar(
                    _select(_func.count()).select_from(AgentTask).where(
                        AgentTask.agent_type == "ingest",
                        AgentTask.status == TaskStatus.pending,
                    )
                )
                if not remaining:
                    break
                logger.info("Maintenance: %d ingest task(s) still pending, starting pass %d",
                            remaining, _pass + 2)
            else:
                break

    logger.info("Maintenance pipeline complete — batch_id=%s", batch_id)


async def run_ingest_pipeline(
    db: AsyncSession,
    force_paths: list[str] | None = None,
    scan_unprocessed: bool = False,
) -> None:
    """File ingest pipeline: scan → per-file ingest → cross-file synthesis → propose.

    Two-round design (Plan B):
      Round 1 — Orchestrator scans files, creates ingest tasks per new/changed file.
                Ingest agents read each file and write summaries.
      Round 2 — Orchestrator reads all summaries, does cross-file planning,
                creates proposer tasks. Proposer generates wiki proposals.

    Args:
        force_paths: If provided, these specific files are re-ingested regardless
                     of whether their content hash has changed (manual /ingest trigger).
        scan_unprocessed: If True, also re-queue files that are currently pending or
                          stuck in processing state (in addition to new/changed/failed).
                          Used by the /ingest/scan command.
    """
    import json
    import uuid

    from core.models.ingest import FileIngestRecord, IngestStatus, compute_file_hash
    from core.settings import settings
    from sqlalchemy import select

    batch_id = str(uuid.uuid4())
    logger.info("Ingest pipeline start — batch_id=%s force=%s", batch_id, force_paths)
    debug_stream.emit("ingest_pipeline_start", batch_id=batch_id, force_paths=force_paths)

    # ── Pre-scan: upsert FileIngestRecord for all allowed files ───────────────
    raw_dirs = settings.file_read_allowed_dirs.strip()
    if not raw_dirs:
        logger.warning("Ingest pipeline: FILE_READ_ALLOWED_DIRS not set — nothing to ingest.")
        return

    import pathlib
    allowed_dirs = [
        pathlib.Path(d.strip()).resolve()
        for d in raw_dirs.split(",")
        if d.strip()
    ]

    # Collect all files under allowed dirs
    all_files: list[pathlib.Path] = []
    for root in allowed_dirs:
        if root.exists() and root.is_dir():
            all_files.extend(p for p in root.rglob("*") if p.is_file())

    # Upsert records and identify which need processing
    needs_processing: list[FileIngestRecord] = []
    force_set = set(str(pathlib.Path(p).resolve()) for p in (force_paths or []))

    for file_path in all_files:
        abs_path = str(file_path)
        try:
            current_hash = compute_file_hash(abs_path)
        except OSError as exc:
            logger.warning("Ingest: cannot hash %s: %s", abs_path, exc)
            continue

        existing = await db.scalar(
            select(FileIngestRecord).where(FileIngestRecord.path == abs_path)
        )
        if existing is None:
            record = FileIngestRecord(path=abs_path, content_hash=current_hash)
            db.add(record)
            await db.flush()
            needs_processing.append(record)
        else:
            existing.last_scanned_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
            changed = existing.content_hash != current_hash
            forced = abs_path in force_set
            stuck = scan_unprocessed and existing.status in (
                IngestStatus.pending, IngestStatus.processing
            )
            if changed or forced or stuck or existing.status == IngestStatus.failed:
                existing.content_hash = current_hash
                existing.status = IngestStatus.pending
                existing.summary = None
                existing.error_message = None
                await db.flush()
                needs_processing.append(existing)

    await db.commit()

    if not needs_processing:
        logger.info("Ingest pipeline: no new or changed files — skipping Round 1.")
    else:
        # ── Round 1: orchestrator plans per-file ingest tasks ─────────────────
        file_list_summary = "\n".join(
            f"  - record_id={r.id} path={r.path}" for r in needs_processing
        )
        orch_msg = (
            f"batch_id: {batch_id}\n\n"
            "INGEST ROUND 1: Create one ingest task per file listed below.\n"
            f"For each file, set agent_type='ingest', batch_id='{batch_id}', and include "
            '{"record_id": "<id>", "path": "<path>"} in context_json.\n\n'
            f"Files to process:\n{file_list_summary}"
        )
        try:
            await _run_agent("orchestrator", orch_msg, db, batch_id=batch_id)
            await db.commit()
        except Exception:
            logger.exception("Ingest Round 1: orchestrator failed — batch_id=%s", batch_id)
            await db.rollback()
            return

        # Mark files as processing
        for record in needs_processing:
            record.status = IngestStatus.processing
        await db.commit()

        # Run ingest agents — retry until no pending ingest tasks remain for this batch
        # (a single agent pass may not process all tasks if max_iterations is reached).
        ingest_agent_id = settings.ingest_agent_id
        ingest_msg = (
            f"batch_id: {batch_id}\n"
            f"ingest_agent_id: {ingest_agent_id}\n\n"
            "Use list_agent_tasks with agent_type=\"ingest\" and status=\"pending\" "
            "(do NOT filter by batch_id — process all pending ingest tasks regardless of source). "
            "For each task, read the file specified in context_json, "
            "extract a summary, and call complete_file_ingest then complete_agent_task."
        )
        from core.models.conversation import AgentTask, TaskStatus
        from sqlalchemy import select as _select, func as _func
        _max_ingest_passes = 10
        for _pass in range(_max_ingest_passes):
            try:
                await _run_agent("ingest", ingest_msg, db, batch_id=batch_id)
                await db.commit()
                logger.info("Ingest Round 1: ingest agent pass %d done — batch_id=%s", _pass + 1, batch_id)
            except Exception:
                logger.exception("Ingest Round 1: ingest agent pass %d failed — batch_id=%s", _pass + 1, batch_id)
                await db.rollback()
                break
            # Check if there are still pending ingest tasks for this batch
            remaining = await db.scalar(
                _select(_func.count()).select_from(AgentTask).where(
                    AgentTask.agent_type == "ingest",
                    AgentTask.batch_id == batch_id,
                    AgentTask.status == TaskStatus.pending,
                )
            )
            if not remaining:
                logger.info("Ingest Round 1: all tasks complete after pass %d — batch_id=%s", _pass + 1, batch_id)
                break
            logger.info("Ingest Round 1: %d task(s) still pending, starting pass %d — batch_id=%s",
                        remaining, _pass + 2, batch_id)

    # ── Round 2: orchestrator reads all summaries, creates proposer tasks ──────
    done_records = await db.scalars(
        select(FileIngestRecord).where(FileIngestRecord.status == IngestStatus.done)
    )
    done_list = list(done_records.all())
    if not done_list:
        logger.info("Ingest pipeline: no done records — skipping Round 2.")
        debug_stream.emit("ingest_pipeline_complete", batch_id=batch_id, proposed=0)
        return

    summaries = "\n".join(
        f"  record_id={r.id} path={r.path}\n  summary={r.summary or '(none)'}"
        for r in done_list
    )
    orch_round2_msg = (
        f"batch_id: {batch_id}\n\n"
        "INGEST ROUND 2: Cross-file synthesis.\n"
        "Below are all processed files with their summaries. "
        "Use list_pages to check the current wiki state, then decide how to group these "
        "files into wiki pages (many-to-many). Create proposer tasks with detailed instructions "
        "including the relevant record_ids and proposed page structure.\n\n"
        f"Processed files:\n{summaries}"
    )
    try:
        await _run_agent("orchestrator", orch_round2_msg, db, batch_id=batch_id)
        await db.commit()
    except Exception:
        logger.exception("Ingest Round 2: orchestrator failed — batch_id=%s", batch_id)
        await db.rollback()
        return

    # Run proposer, reviewer, executor for ingest-generated proposals
    for agent_type in ("proposer", "reviewer", "executor"):
        agent_id = getattr(settings, f"{agent_type}_agent_id")
        _extra_ingest = {
            "reviewer": (
                'Also use list_proposals with status="pending" (no batch_id filter) '
                "to find any proposals awaiting review, and review them."
            ),
            "executor": (
                'Also use list_proposals with status="approved" (no batch_id filter) '
                "to find any approved proposals, and apply them."
            ),
        }
        extra = _extra_ingest.get(agent_type, "")
        specialist_msg = (
            f"batch_id: {batch_id}\n"
            f"{agent_type}_agent_id: {agent_id}\n\n"
            f'Use list_agent_tasks with agent_type="{agent_type}" and status="pending" '
            f'to find ALL your pending tasks (do NOT filter by batch_id). '
            f'Execute each task (your agent id is "{agent_id}"). '
            + (f"{extra} " if extra else "")
            + 'Call complete_agent_task with outcome="done" or "failed" for each task. '
            "If there are no tasks for you, do nothing."
        )
        try:
            await _run_agent(agent_type, specialist_msg, db, batch_id=batch_id)
            await db.commit()
            logger.info("Ingest Round 2: %s done — batch_id=%s", agent_type, batch_id)
        except Exception:
            logger.exception("Ingest Round 2: %s failed — batch_id=%s", agent_type, batch_id)
            await db.rollback()

    debug_stream.emit("ingest_pipeline_complete", batch_id=batch_id)
    logger.info("Ingest pipeline complete — batch_id=%s", batch_id)
