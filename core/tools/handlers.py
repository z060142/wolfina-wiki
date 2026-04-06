"""Tool handler dispatch layer.

Each handler receives the tool input dict and a live AsyncSession,
calls the appropriate core service, and returns a plain dict that will be
JSON-serialised and sent back to the LLM as a tool_result.

dispatch_tool() is the single entry point used by the LLM service.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
    return {"proposal": _proposal_out(proposal)}


async def _propose_page_edit(inp: dict, db: AsyncSession) -> dict:
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
    return {"proposal": _proposal_out(proposal)}


async def _review_proposal(inp: dict, db: AsyncSession) -> dict:
    from core.models.proposal import ReviewDecision
    data = ReviewRequest(
        reviewer_agent_id=inp["reviewer_agent_id"],
        decision=ReviewDecision(inp["decision"]),
        feedback=inp.get("feedback"),
    )
    proposal = await proposal_service.review_proposal(db, inp["proposal_id"], data)
    return {"proposal": _proposal_out(proposal)}


async def _apply_proposal(inp: dict, db: AsyncSession) -> dict:
    data = ApplyRequest(executor_agent_id=inp["executor_agent_id"])
    proposal = await proposal_service.apply_proposal(db, inp["proposal_id"], data)
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
    task = AgentTask(
        agent_type=inp["agent_type"],
        instruction=inp["instruction"],
        context_json=inp.get("context_json"),
        batch_id=inp.get("batch_id"),
        status=TaskStatus.pending,
    )
    db.add(task)
    await db.flush()
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
    return {"task": _task_out(task)}


# ── dispatch table ────────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "search_pages": _search_pages,
    "get_page": _get_page,
    "list_pages": _list_pages,
    "get_related_pages": _get_related_pages,
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
        logger.warning("Tool %s raised %s: %s", tool_name, type(exc).__name__, exc)
        return {"error": str(exc)}
