import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class IngestStatus(str, Enum):
    pending = "pending"        # discovered, not yet processed
    processing = "processing"  # ingest agent currently reading it
    done = "done"              # summary written, ready for orchestrator planning
    failed = "failed"          # ingest agent reported failure


class FileIngestRecord(Base):
    """Tracks every file the ingest pipeline has seen.

    Lifecycle:
        pending → processing → done
                            ↘ failed

    The orchestrator scans allowed dirs via list_files, compares content_hash,
    and upserts records.  The ingest agent sets status=done and writes a summary.
    The orchestrator uses the summary in Round 2 for cross-file planning without
    needing to re-read the raw file.
    """

    __tablename__ = "file_ingest_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)

    # Absolute resolved path — unique identifier for a file on disk
    path: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)

    # SHA-256 of raw file bytes at the time of last scan
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String, nullable=False, default=IngestStatus.pending
    )

    # Short description written by the ingest agent after reading the file.
    # Used by orchestrator for cross-file planning without re-reading raw files.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON array of page UUIDs this file contributed to, e.g. '["uuid1","uuid2"]'
    related_page_ids: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Human-readable error written by ingest agent on failure
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def compute_file_hash(path: str) -> str:
    """Return the SHA-256 hex digest of a file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
