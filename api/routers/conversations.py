"""HTTP endpoints for conversation window management.

External programs send messages here; the wiki accumulates them and
automatically flushes to the LLM pipeline when conditions are met.

Endpoints:
  POST   /conversations/windows              — create a new window
  GET    /conversations/windows              — list windows
  GET    /conversations/windows/{id}         — get window status
  POST   /conversations/windows/{id}/messages — add a message (may trigger flush)
  GET    /conversations/windows/{id}/messages — list messages
  POST   /conversations/windows/{id}/flush   — manual flush
  GET    /conversations/scheduler/status     — scheduler diagnostics
  GET    /conversations/tasks                — list agent tasks
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.schemas.conversation import (
    AddMessageResponse,
    MessageAdd,
    MessageOut,
    TaskOut,
    WindowCreate,
    WindowOut,
)
from core.services import conversation_service

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ── windows ───────────────────────────────────────────────────────────────────

@router.post("/windows", response_model=WindowOut, status_code=201)
async def create_window(
    data: WindowCreate,
    db: AsyncSession = Depends(get_db),
) -> WindowOut:
    window = await conversation_service.create_window(db, data.external_source_id)
    return WindowOut.model_validate(window)


@router.get("/windows", response_model=list[WindowOut])
async def list_windows(
    status: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> list[WindowOut]:
    windows = await conversation_service.list_windows(db, status=status, limit=limit)
    return [WindowOut.model_validate(w) for w in windows]


@router.get("/windows/{window_id}", response_model=WindowOut)
async def get_window(
    window_id: str,
    db: AsyncSession = Depends(get_db),
) -> WindowOut:
    window = await conversation_service.get_window(db, window_id)
    return WindowOut.model_validate(window)


# ── messages ──────────────────────────────────────────────────────────────────

@router.post("/windows/{window_id}/messages", response_model=AddMessageResponse, status_code=201)
async def add_message(
    window_id: str,
    data: MessageAdd,
    db: AsyncSession = Depends(get_db),
) -> AddMessageResponse:
    msg, should_flush = await conversation_service.add_message(db, window_id, data)

    flush_triggered = False
    if should_flush:
        await conversation_service.trigger_flush(window_id)
        flush_triggered = True

    return AddMessageResponse(
        message=MessageOut.model_validate(msg),
        flush_triggered=flush_triggered,
    )


@router.get("/windows/{window_id}/messages", response_model=list[MessageOut])
async def list_messages(
    window_id: str,
    include_processed: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    messages = await conversation_service.get_messages(
        db, window_id, include_processed=include_processed
    )
    return [MessageOut.model_validate(m) for m in messages]


# ── manual flush ──────────────────────────────────────────────────────────────

@router.post("/windows/{window_id}/flush", status_code=202)
async def flush_window(
    window_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    await conversation_service.manual_flush(db, window_id)
    return {"status": "flush_queued", "window_id": window_id}


# ── scheduler diagnostics ─────────────────────────────────────────────────────

@router.get("/scheduler/status")
async def scheduler_status() -> dict:
    from core.services.scheduler_service import scheduler
    from core.settings import settings

    return {
        "flush_rate_in_window": scheduler.flush_rate(),
        "window_hours": settings.scheduler_rate_window_hours,
        "current_interval_seconds": scheduler.current_interval(),
        "min_interval_seconds": settings.scheduler_min_interval_seconds,
        "max_interval_seconds": settings.scheduler_max_interval_seconds,
    }


# ── agent tasks ───────────────────────────────────────────────────────────────

@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    status: str | None = None,
    agent_type: str | None = None,
    batch_id: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> list[TaskOut]:
    from sqlalchemy import select
    from core.models.conversation import AgentTask

    stmt = select(AgentTask)
    if status:
        stmt = stmt.where(AgentTask.status == status)
    if agent_type:
        stmt = stmt.where(AgentTask.agent_type == agent_type)
    if batch_id:
        stmt = stmt.where(AgentTask.batch_id == batch_id)
    stmt = stmt.order_by(AgentTask.created_at.desc()).limit(limit)
    result = await db.scalars(stmt)
    tasks = list(result.all())
    return [TaskOut.model_validate(t) for t in tasks]
