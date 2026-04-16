"""MaintenanceIssue — deterministically detected problems awaiting triage.

Scanners (core/services/maintenance_scanners.py) populate this table by
querying the DB directly, with no LLM involvement.  The orchestrator then
reads the open issue list and decides which ones warrant creating agent tasks.

Issue lifecycle:
    open → in_progress → resolved
                      ↘ ignored
"""

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class IssueStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    ignored = "ignored"


class IssueSeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


# Numeric score per severity — used for sorting & threshold filtering.
SEVERITY_SCORES: dict[str, float] = {
    IssueSeverity.low: 0.25,
    IssueSeverity.medium: 0.5,
    IssueSeverity.high: 0.75,
    IssueSeverity.critical: 1.0,
}


class MaintenanceIssue(Base):
    """One detected maintenance problem, uniquely identified by ``fingerprint``.

    The fingerprint is a deterministic hash of (issue_type, target_id[, detail_key])
    so re-running the same scanner never creates a duplicate row.  Instead, the
    upsert logic in maintenance_scanners.py skips fingerprints that already have
    status=open or in_progress.  If a previously resolved issue re-emerges, the
    scanner recreates it as a fresh open row.

    ``suppress_until`` lets the orchestrator snooze low-priority issues: a scanner
    will not recreate a suppressed issue until after that timestamp.
    """

    __tablename__ = "maintenance_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # ── Classification ────────────────────────────────────────────────────────
    # Canonical issue types (see maintenance_scanners.py for full list):
    #   missing_summary, stub_page, orphan_page,
    #   duplicate_candidate, ingest_backlog
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # What kind of object is affected: page / proposal / ingest_record
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # UUID of the affected object
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # ── Severity / priority ───────────────────────────────────────────────────
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IssueSeverity.medium
    )
    # Numeric priority (higher = more urgent).  Derived from severity but may be
    # adjusted by scanners when additional signals are available.
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    # ── Description ───────────────────────────────────────────────────────────
    # Human-readable sentence describing what was detected.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Supporting data (JSON string): page titles, content lengths, record paths…
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Deduplication ─────────────────────────────────────────────────────────
    # Stable hash: sha1(issue_type + ":" + target_id [+ ":" + detail_key])[:16]
    # UNIQUE constraint prevents duplicate rows across scanner runs.
    fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IssueStatus.open, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When set, the scanner skips recreating this issue until after this timestamp.
    # Useful for snoozing low-priority items that can't be fixed immediately.
    suppress_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
