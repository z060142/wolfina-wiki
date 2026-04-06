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

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from core.debug.event_stream import debug_stream
from core.services.llm_service import run_tool_loop
from core.settings import settings
from core.tools.definitions import get_tools_for_agent

logger = logging.getLogger(__name__)


# ── system prompts ────────────────────────────────────────────────────────────

_PROPOSER_PROMPT = """\
You are the Proposer agent for the Wolfina Wiki system.
Your job is to analyse a conversation and extract knowledge worth storing in the wiki.

For each piece of knowledge:
1. Use search_pages to check if a relevant page already exists.
2. If it exists, use propose_page_edit to update it with new information.
3. If it does not exist, use propose_new_page to create it.

Guidelines:
- Prefer editing existing pages over creating new ones.
- Each proposal must have a clear rationale explaining what was learned and why it matters.
- Use the provided batch_id on every proposal for traceability.
- Be conservative: only propose things clearly supported by the conversation.
- Do not propose duplicate pages — always search first.
"""

_REVIEWER_PROMPT = """\
You are the Reviewer agent for the Wolfina Wiki system.
Your job is to review pending edit proposals and approve or reject them.

For each pending proposal in the current batch:
1. Use list_proposals to find pending proposals for this batch.
2. Use get_page to read the current page content (for edits).
3. Use get_page_history to check recent changes.
4. Decide: approve if the content is accurate, well-structured, and non-conflicting;
   reject if it contains errors, duplicates existing content, or lacks rationale.
5. Use review_proposal with your decision and optional feedback.

You must not be the same agent as the proposer.
Be thorough but decisive — a good wiki benefits from timely, high-quality updates.
"""

_EXECUTOR_PROMPT = """\
You are the Executor agent for the Wolfina Wiki system.
Your job is to apply approved proposals to the wiki.

Steps:
1. Use list_proposals with status='approved' and the provided batch_id.
2. For each approved proposal, use apply_proposal with your executor_agent_id.

You must not be the same agent as the proposer or any reviewer.
Do not modify proposal content — apply them exactly as approved.
"""

_RELATION_PROMPT = """\
You are the Relation agent for the Wolfina Wiki system.
Your job is to enrich the wiki's knowledge graph by adding relations between pages.

For the newly created or updated pages in the current batch:
1. Use list_pages or search_pages to find pages relevant to each new/updated page.
2. Use get_related_pages to check what relations already exist.
3. Use add_page_relation to add appropriate links.

Relation types:
- parent / child: for hierarchical topics (e.g. Python → Python Basics)
- related_to: for semantically related topics at the same level
- references: when one page cites or is derived from another

Do not add redundant relations that already exist.
"""

_RESEARCH_PROMPT = """\
You are the Research agent for the Wolfina Wiki system.
Your job is to gather and summarise information from the wiki in response to a specific task.

Use search_pages, get_page, list_pages, get_related_pages, and get_page_history
to collect relevant information.

Produce a clear, structured summary of what you found.
Mark your task as done using complete_agent_task when finished.
"""

_ORCHESTRATOR_PROMPT = """\
You are the Orchestrator agent for the Wolfina Wiki system.
Your job is to evaluate the current state of the wiki and create a prioritised work queue
for specialist agents (research, proposer, reviewer, executor, relation).

Steps:
1. Use list_pages to review recently updated pages.
2. Use list_proposals to check for stale pending/approved proposals.
3. Use list_agent_tasks to see what is already queued.
4. Create tasks using create_agent_task for work that needs to be done.

Example tasks you might create:
- Ask research agent to summarise orphaned pages
- Ask proposer agent to merge near-duplicate pages
- Ask reviewer agent to review long-pending proposals
- Ask executor agent to apply all approved proposals
- Ask relation agent to link recently created pages

Be specific in the instruction for each task — include page IDs or search terms as context_json.
Do not create duplicate tasks if equivalent ones are already pending.
"""


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
    """
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

    # 4. Relation agent
    relation_msg = (
        f"batch_id: {batch_id}\n"
        f"relation_agent_id: {settings.relation_agent_id}\n\n"
        f"Add relations for pages created or updated in batch_id={batch_id}."
    )
    try:
        await _run_agent("relation", relation_msg, db, batch_id=batch_id)
        await db.commit()
        logger.info("Flush pipeline: relation done — batch_id=%s", batch_id)
    except Exception:
        logger.exception("Flush pipeline: relation failed — batch_id=%s", batch_id)
        await db.rollback()

    logger.info("Flush pipeline complete — batch_id=%s", batch_id)


async def run_maintenance_pipeline(db: AsyncSession) -> None:
    """Scheduled maintenance: orchestrator creates tasks, then specialists execute them.

    Runs: orchestrator → research → proposer → reviewer → executor → relation
    Each specialist only processes tasks the orchestrator created for it.
    """
    import uuid
    batch_id = str(uuid.uuid4())
    logger.info("Maintenance pipeline start — batch_id=%s", batch_id)

    # 1. Orchestrator builds the work queue
    orch_msg = (
        f"batch_id: {batch_id}\n\n"
        "Evaluate the current wiki state and create tasks for specialist agents as needed."
    )
    try:
        await _run_agent("orchestrator", orch_msg, db, batch_id=batch_id)
        await db.commit()
    except Exception:
        logger.exception("Maintenance: orchestrator failed — batch_id=%s", batch_id)
        await db.rollback()
        return

    # 2. Run each specialist against its pending tasks in this batch
    for agent_type in ("research", "proposer", "reviewer", "executor", "relation"):
        agent_id = getattr(settings, f"{agent_type}_agent_id")
        specialist_msg = (
            f"batch_id: {batch_id}\n"
            f"{agent_type}_agent_id: {agent_id}\n\n"
            f"List your pending tasks for batch_id={batch_id} and execute each one. "
            "Mark each task done or failed when finished."
        )
        try:
            await _run_agent(agent_type, specialist_msg, db, batch_id=batch_id)
            await db.commit()
            logger.info("Maintenance: %s done — batch_id=%s", agent_type, batch_id)
        except Exception:
            logger.exception("Maintenance: %s failed — batch_id=%s", agent_type, batch_id)
            await db.rollback()

    logger.info("Maintenance pipeline complete — batch_id=%s", batch_id)
