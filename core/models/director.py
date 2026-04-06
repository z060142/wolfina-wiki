"""DirectorSession — persistent context window for the Director super-agent."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class DirectorSession(Base):
    __tablename__ = "director_sessions"

    id: Mapped[str] = mapped_column(
        primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(default="New Session")

    # JSON-encoded list of {role, content, tool_calls?, name?, tool_call_id?}
    # Only user/assistant/tool messages — system prompt is rebuilt fresh each turn.
    messages: Mapped[str] = mapped_column(Text, default="[]")

    # JSON-encoded list of {id, text, done, created_at}
    todo_list: Mapped[str] = mapped_column(Text, default="[]")

    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
