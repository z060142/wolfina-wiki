import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class WindowStatus(str, Enum):
    active = "active"
    flushing = "flushing"
    cleared = "cleared"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class ConversationWindow(Base):
    """A rolling window of conversation messages from an external source.

    Messages accumulate until a flush condition is met (count / chars / time),
    at which point the LLM pipeline processes them into wiki entries and marks
    all messages as processed.
    """

    __tablename__ = "conversation_windows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    external_source_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=WindowStatus.active)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list["ConversationMessage"]] = relationship(
        "ConversationMessage",
        back_populates="window",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.sequence_no",
    )


class ConversationMessage(Base):
    """Single message inside a ConversationWindow.

    Marked processed=True after the flush pipeline has digested it.
    The record is kept for audit purposes.
    """

    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    window_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_windows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)   # user / assistant / system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    window: Mapped["ConversationWindow"] = relationship("ConversationWindow", back_populates="messages")


class AgentTask(Base):
    """A work item created by the orchestrator agent and consumed by a specialist agent.

    agent_type values: research / proposer / reviewer / executor / relation
    context_json: arbitrary JSON string providing task-specific context (page IDs, batch ID, etc.)
    """

    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TaskStatus.pending)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Incremented each time the janitor re-queues this task. Capped at janitor_max_task_retries.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
