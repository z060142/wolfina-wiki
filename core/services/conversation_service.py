"""Conversation window management.

A ConversationWindow collects messages from an external source (another script,
UI, or IPC client).  When any flush condition is satisfied, the window is handed
off to the LLM agent pipeline which converts the conversation into wiki entries.
After processing, messages are marked processed=True (kept for audit).

Flush conditions (ANY one triggers):
  - message_count  >= settings.flush_max_messages
  - total_char_count >= settings.flush_max_chars
  - seconds since first message >= settings.flush_max_seconds
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.base import AsyncSessionLocal
from core.debug.event_stream import debug_stream
from core.exceptions import Conflict, NotFound
from core.models.conversation import ConversationMessage, ConversationWindow, WindowStatus
from core.schemas.conversation import MessageAdd
from core.settings import settings

logger = logging.getLogger(__name__)


# ── window CRUD ───────────────────────────────────────────────────────────────

async def create_window(db: AsyncSession, external_source_id: str = "") -> ConversationWindow:
    window = ConversationWindow(external_source_id=external_source_id)
    db.add(window)
    await db.flush()
    debug_stream.emit(
        "window_created",
        window_id=window.id,
        external_source_id=external_source_id,
        status=window.status,
    )
    return window


async def get_window(db: AsyncSession, window_id: str) -> ConversationWindow:
    window = await db.scalar(
        select(ConversationWindow)
        .where(ConversationWindow.id == window_id)
        .options(selectinload(ConversationWindow.messages))
    )
    if window is None:
        raise NotFound(f"Window '{window_id}' not found.")
    return window


async def list_windows(db: AsyncSession, *, status: str | None = None, limit: int = 50) -> list[ConversationWindow]:
    stmt = select(ConversationWindow)
    if status:
        stmt = stmt.where(ConversationWindow.status == status)
    stmt = stmt.order_by(ConversationWindow.created_at.desc()).limit(limit)
    result = await db.scalars(stmt)
    return list(result.all())


# ── message management ────────────────────────────────────────────────────────

async def add_message(
    db: AsyncSession,
    window_id: str,
    data: MessageAdd,
) -> tuple[ConversationMessage, bool]:
    """Add a message to the window. Returns (message, should_flush).

    should_flush=True means a flush condition was just triggered.
    The caller is responsible for actually running the flush (typically
    as a background task so the HTTP response is not blocked).
    """
    window = await db.scalar(
        select(ConversationWindow).where(ConversationWindow.id == window_id)
    )
    if window is None:
        raise NotFound(f"Window '{window_id}' not found.")
    if window.status == WindowStatus.flushing:
        raise Conflict("Window is currently being flushed. Wait for it to become active again.")
    if window.status == WindowStatus.cleared:
        # Auto-reactivate a cleared window so it can accept new messages.
        window.status = WindowStatus.active
        window.message_count = 0
        window.total_char_count = 0
        window.first_message_at = None

    char_count = len(data.content)
    now = datetime.now(timezone.utc)

    msg = ConversationMessage(
        window_id=window_id,
        role=data.role,
        content=data.content,
        char_count=char_count,
        sequence_no=window.message_count,
    )
    window.message_count += 1
    window.total_char_count += char_count
    if window.first_message_at is None:
        window.first_message_at = now
    window.last_message_at = now

    db.add(msg)
    await db.flush()

    # Only flush after an assistant turn — flushing mid-exchange (on a user message)
    # would capture an incomplete conversation and race with the LLM response write.
    should_flush = data.role == "assistant" and _check_flush_conditions(window)
    debug_stream.emit(
        "message_added",
        window_id=window_id,
        message_id=msg.id,
        role=data.role,
        char_count=char_count,
        message_count=window.message_count,
        total_char_count=window.total_char_count,
        flush_triggered=should_flush,
    )
    return msg, should_flush


def _check_flush_conditions(window: ConversationWindow) -> bool:
    if window.status != WindowStatus.active:
        return False
    if window.message_count >= settings.flush_max_messages:
        return True
    if window.total_char_count >= settings.flush_max_chars:
        return True
    if window.first_message_at:
        first = window.first_message_at
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - first).total_seconds()
        if elapsed >= settings.flush_max_seconds:
            return True
    return False


async def get_messages(
    db: AsyncSession,
    window_id: str,
    *,
    include_processed: bool = True,
) -> list[ConversationMessage]:
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.window_id == window_id)
        .order_by(ConversationMessage.sequence_no.asc())
    )
    if not include_processed:
        stmt = stmt.where(ConversationMessage.processed == False)  # noqa: E712
    result = await db.scalars(stmt)
    return list(result.all())


# ── flush logic ───────────────────────────────────────────────────────────────

def _format_conversation(messages: list[ConversationMessage]) -> str:
    lines = []
    for msg in messages:
        if not msg.processed:
            lines.append(f"[{msg.role.upper()}]: {msg.content}")
    return "\n".join(lines)


async def trigger_flush(window_id: str) -> None:
    """Launch the flush pipeline as an independent background task.

    Opens its own DB session so it does not interfere with the caller's session.
    Records flush timestamp in the scheduler for dynamic interval calculation.
    """
    asyncio.create_task(_flush_background(window_id))


async def _flush_background(window_id: str) -> None:
    """Background coroutine that actually runs the flush pipeline."""
    from core.services.agent_service import run_flush_pipeline
    from core.services.scheduler_service import scheduler

    # Step 1: mark window as flushing (own session + transaction).
    async with AsyncSessionLocal() as db:
        async with db.begin():
            window = await db.scalar(
                select(ConversationWindow)
                .where(ConversationWindow.id == window_id)
                .with_for_update()
            )
            if window is None or window.status != WindowStatus.active:
                return
            window.status = WindowStatus.flushing
        # session closes and commits here

    # Step 2: read unprocessed messages (own session).
    async with AsyncSessionLocal() as db:
        async with db.begin():
            messages = await get_messages(db, window_id, include_processed=False)
            if not messages:
                # Nothing to flush — reset to active.
                window = await db.scalar(
                    select(ConversationWindow).where(ConversationWindow.id == window_id)
                )
                if window:
                    window.status = WindowStatus.active
                return
            conversation_text = _format_conversation(messages)
            batch_id = str(uuid.uuid4())

    logger.info("Flush started — window_id=%s batch_id=%s", window_id, batch_id)
    debug_stream.emit("flush_started", window_id=window_id, batch_id=batch_id, message_count=len(messages))

    # Run the agent pipeline in its own transaction scope.
    async with AsyncSessionLocal() as pipeline_db:
        try:
            await run_flush_pipeline(conversation_text, batch_id, pipeline_db)
        except Exception:
            logger.exception("Flush pipeline error — window_id=%s", window_id)

    # Mark messages processed and window cleared.
    async with AsyncSessionLocal() as db:
        async with db.begin():
            msgs = await db.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.window_id == window_id,
                    ConversationMessage.processed == False,  # noqa: E712
                )
            )
            for m in msgs.all():
                m.processed = True

            window = await db.scalar(
                select(ConversationWindow).where(ConversationWindow.id == window_id)
            )
            if window:
                window.status = WindowStatus.cleared

    logger.info("Flush complete — window_id=%s batch_id=%s", window_id, batch_id)
    debug_stream.emit("flush_completed", window_id=window_id, batch_id=batch_id)

    # Notify scheduler so it can update its dynamic interval.
    scheduler.record_flush()


async def manual_flush(db: AsyncSession, window_id: str) -> None:
    """HTTP-triggered manual flush. Commits the status change then fires background task."""
    window = await db.scalar(
        select(ConversationWindow).where(ConversationWindow.id == window_id)
    )
    if window is None:
        raise NotFound(f"Window '{window_id}' not found.")
    if window.status == WindowStatus.flushing:
        raise Conflict("Window is already flushing.")
    # The actual flush runs as a background task after commit.
    await trigger_flush(window_id)
