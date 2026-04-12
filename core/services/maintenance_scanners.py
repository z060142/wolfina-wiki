"""Deterministic maintenance scanners — no LLM involved.

Each scanner queries the database, applies fixed rules, and returns a list of
MaintenanceIssue objects that describe what was found.  The caller
(run_maintenance_pipeline) upserts them into the DB via ``upsert_issues``,
then passes a digest of the open issues to the orchestrator agent.

Scanner inventory:
  scan_page_completeness   — missing summary, stub content
  scan_orphan_pages        — pages with zero relations
  scan_duplicate_candidates — pairs of pages whose titles are suspiciously similar
  scan_ingest_backlog      — FileIngestRecord rows stuck in pending/processing

Public API (called from agent_service):
  run_all_scanners(db)     → list[MaintenanceIssue]   (upserted, deduplicated)
  format_issue_digest(issues) → str                   (text for orchestrator)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models.maintenance import (
    SEVERITY_SCORES,
    IssueSeverity,
    IssueStatus,
    MaintenanceIssue,
)
from core.models.page import Page, PageRelation, PageStatus
from core.settings import settings

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Fingerprint helper ────────────────────────────────────────────────────────

def _fingerprint(issue_type: str, target_id: str, detail: str = "") -> str:
    """Stable 16-char hex fingerprint for deduplication."""
    raw = f"{issue_type}:{target_id}"
    if detail:
        raw += f":{detail}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _make_issue(
    *,
    issue_type: str,
    target_type: str,
    target_id: str,
    severity: str,
    reason: str,
    evidence: dict | None = None,
    detail: str = "",
) -> MaintenanceIssue:
    fp = _fingerprint(issue_type, target_id, detail)
    return MaintenanceIssue(
        issue_type=issue_type,
        target_type=target_type,
        target_id=target_id,
        severity=severity,
        score=SEVERITY_SCORES[severity],
        reason=reason,
        evidence_json=json.dumps(evidence) if evidence else None,
        fingerprint=fp,
        status=IssueStatus.open,
    )


# ── Scanner: page completeness ────────────────────────────────────────────────

async def scan_page_completeness(db: AsyncSession) -> list[MaintenanceIssue]:
    """Flag active pages that have no summary or suspiciously short content."""
    grace_cutoff = _now() - timedelta(minutes=settings.maintenance_new_page_grace_minutes)

    rows = (await db.scalars(
        select(Page).where(
            Page.status == PageStatus.active,
            Page.created_at < grace_cutoff,
        )
    )).all()

    issues: list[MaintenanceIssue] = []
    for page in rows:
        summary_empty = not page.summary or not page.summary.strip()
        content_short = len((page.content or "").strip()) < settings.maintenance_stub_page_min_chars

        if summary_empty:
            issues.append(_make_issue(
                issue_type="missing_summary",
                target_type="page",
                target_id=page.id,
                severity=IssueSeverity.medium,
                reason=f"Page '{page.title}' (slug={page.slug}) has no summary.",
                evidence={"page_id": page.id, "title": page.title, "slug": page.slug},
            ))
        elif content_short:
            char_count = len((page.content or "").strip())
            issues.append(_make_issue(
                issue_type="stub_page",
                target_type="page",
                target_id=page.id,
                severity=IssueSeverity.low,
                reason=(
                    f"Page '{page.title}' (slug={page.slug}) is a stub "
                    f"({char_count} chars, threshold={settings.maintenance_stub_page_min_chars})."
                ),
                evidence={
                    "page_id": page.id,
                    "title": page.title,
                    "slug": page.slug,
                    "char_count": char_count,
                },
            ))

    return issues


# ── Scanner: orphan pages ─────────────────────────────────────────────────────

async def scan_orphan_pages(db: AsyncSession) -> list[MaintenanceIssue]:
    """Flag active pages that have no outgoing or incoming relations."""
    grace_cutoff = _now() - timedelta(minutes=settings.maintenance_new_page_grace_minutes)

    # Pages that have at least one relation (either direction)
    pages_with_relations = (await db.scalars(
        select(PageRelation.source_page_id)
        .union(select(PageRelation.target_page_id))
    )).all()
    linked_ids: set[str] = set(pages_with_relations)

    all_pages = (await db.scalars(
        select(Page).where(
            Page.status == PageStatus.active,
            Page.created_at < grace_cutoff,
        )
    )).all()

    issues: list[MaintenanceIssue] = []
    for page in all_pages:
        if page.id not in linked_ids:
            issues.append(_make_issue(
                issue_type="orphan_page",
                target_type="page",
                target_id=page.id,
                severity=IssueSeverity.low,
                reason=f"Page '{page.title}' (slug={page.slug}) has no relations to other pages.",
                evidence={"page_id": page.id, "title": page.title, "slug": page.slug},
            ))

    return issues


# ── Scanner: duplicate candidates ─────────────────────────────────────────────

def _title_similarity(a: str, b: str) -> float:
    """Normalised [0,1] similarity ratio between two page titles."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


async def scan_duplicate_candidates(db: AsyncSession) -> list[MaintenanceIssue]:
    """Flag pairs of active pages whose titles are suspiciously similar.

    Only surfaces candidate pairs — the orchestrator must use compare_pages
    to confirm before taking any action.  This keeps the LLM workload bounded
    and focused.
    """
    _SIMILARITY_THRESHOLD = 0.72  # empirically: catches plurals/synonyms, avoids noise

    all_pages = (await db.scalars(
        select(Page).where(Page.status == PageStatus.active)
    )).all()

    issues: list[MaintenanceIssue] = []
    seen_pairs: set[frozenset[str]] = set()

    for i, a in enumerate(all_pages):
        for b in all_pages[i + 1:]:
            pair = frozenset({a.id, b.id})
            if pair in seen_pairs:
                continue
            sim = _title_similarity(a.title, b.title)
            if sim >= _SIMILARITY_THRESHOLD:
                seen_pairs.add(pair)
                # Use the lexically smaller id as the canonical target so the
                # fingerprint is stable regardless of page ordering.
                primary, secondary = (a, b) if a.id < b.id else (b, a)
                issues.append(_make_issue(
                    issue_type="duplicate_candidate",
                    target_type="page",
                    target_id=primary.id,
                    severity=IssueSeverity.medium,
                    reason=(
                        f"Pages '{a.title}' and '{b.title}' have similar titles "
                        f"(similarity={sim:.2f}) and may be duplicates."
                    ),
                    evidence={
                        "page_a_id": a.id,
                        "page_a_title": a.title,
                        "page_b_id": b.id,
                        "page_b_title": b.title,
                        "similarity": round(sim, 3),
                    },
                    detail=secondary.id,  # included in fingerprint to distinguish pairs
                ))

    return issues


# ── Scanner: ingest backlog ───────────────────────────────────────────────────

async def scan_ingest_backlog(db: AsyncSession) -> list[MaintenanceIssue]:
    """Flag FileIngestRecord rows that have been pending/processing too long."""
    try:
        from core.models.ingest import FileIngestRecord, IngestStatus
    except ImportError:
        return []

    stuck_cutoff = _now() - timedelta(minutes=settings.maintenance_ingest_stuck_minutes)

    stuck = (await db.scalars(
        select(FileIngestRecord).where(
            FileIngestRecord.status.in_([IngestStatus.pending, IngestStatus.processing]),
            FileIngestRecord.last_scanned_at < stuck_cutoff,
        )
    )).all()

    issues: list[MaintenanceIssue] = []
    for rec in stuck:
        issues.append(_make_issue(
            issue_type="ingest_backlog",
            target_type="ingest_record",
            target_id=rec.id,
            severity=IssueSeverity.medium,
            reason=(
                f"Ingest record for '{rec.path}' has been in status='{rec.status}' "
                f"since {rec.last_scanned_at.strftime('%Y-%m-%d %H:%M')} UTC."
            ),
            evidence={"record_id": rec.id, "path": rec.path, "status": rec.status},
        ))

    return issues


# ── Upsert: persist issues with fingerprint deduplication ────────────────────

async def upsert_issues(
    db: AsyncSession,
    candidates: list[MaintenanceIssue],
) -> list[MaintenanceIssue]:
    """Insert new issues; skip fingerprints that are already open/in_progress.

    If a fingerprint was previously resolved or ignored and the problem
    recurred, it is recreated as a fresh open row (old row stays, new row added
    with a fresh id — the unique constraint on fingerprint is bypassed because
    we first delete stale resolved/ignored rows for the same fingerprint).

    Returns the list of issues that were actually inserted (newly detected).
    """
    if not candidates:
        return []

    now = _now()
    inserted: list[MaintenanceIssue] = []

    for issue in candidates:
        # Skip issues suppressed until a future time
        existing = await db.scalar(
            select(MaintenanceIssue).where(
                MaintenanceIssue.fingerprint == issue.fingerprint
            )
        )
        if existing is not None:
            if existing.suppress_until and existing.suppress_until > now:
                logger.debug("Scanner: skipping suppressed issue fp=%s", issue.fingerprint)
                continue
            if existing.status in (IssueStatus.open, IssueStatus.in_progress):
                logger.debug(
                    "Scanner: issue already open fp=%s type=%s",
                    issue.fingerprint, issue.issue_type,
                )
                continue
            # Previously resolved/ignored — delete the old row so we can insert afresh
            await db.delete(existing)
            await db.flush()

        db.add(issue)
        inserted.append(issue)

    if inserted:
        await db.flush()
        logger.info(
            "Scanners: upserted %d new issue(s): %s",
            len(inserted),
            [i.issue_type for i in inserted],
        )

    return inserted


# ── Resolve helper ────────────────────────────────────────────────────────────

async def resolve_issues_for_target(db: AsyncSession, target_id: str) -> int:
    """Mark all open issues for a given target_id as resolved.

    Called automatically after the orchestrator creates a task for an issue,
    so the issue doesn't pile up across maintenance cycles.
    Returns the count of issues resolved.
    """
    rows = (await db.scalars(
        select(MaintenanceIssue).where(
            MaintenanceIssue.target_id == target_id,
            MaintenanceIssue.status.in_([IssueStatus.open, IssueStatus.in_progress]),
        )
    )).all()

    now = _now()
    for row in rows:
        row.status = IssueStatus.resolved
        row.resolved_at = now

    return len(rows)


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_all_scanners(db: AsyncSession) -> list[MaintenanceIssue]:
    """Run every scanner, upsert results, and return the full list of open issues.

    This function is called at the start of run_maintenance_pipeline() BEFORE
    the orchestrator agent runs.  The orchestrator receives the result as a
    pre-digested issue list rather than having to explore the wiki freely.
    """
    candidates: list[MaintenanceIssue] = []

    for scanner_fn in (
        scan_page_completeness,
        scan_orphan_pages,
        scan_duplicate_candidates,
        scan_ingest_backlog,
    ):
        try:
            found = await scanner_fn(db)
            candidates.extend(found)
            logger.debug("Scanner %s: %d issue(s)", scanner_fn.__name__, len(found))
        except Exception:
            logger.exception("Scanner %s raised an error — skipping", scanner_fn.__name__)

    await upsert_issues(db, candidates)
    await db.commit()

    # Return all open issues (may include pre-existing ones from prior cycles)
    open_issues = (await db.scalars(
        select(MaintenanceIssue)
        .where(MaintenanceIssue.status == IssueStatus.open)
        .order_by(MaintenanceIssue.score.desc(), MaintenanceIssue.created_at.asc())
    )).all()

    logger.info("Scanners complete: %d open issue(s) total", len(open_issues))
    return list(open_issues)


# ── Digest formatter ──────────────────────────────────────────────────────────

def format_issue_digest(issues: list[MaintenanceIssue], max_issues: int = 30) -> str:
    """Render open issues as a compact numbered list for the orchestrator's user message.

    Limited to ``max_issues`` to keep the prompt bounded.  Issues are pre-sorted
    by score (highest first) by ``run_all_scanners``.
    """
    if not issues:
        return "No open maintenance issues detected by automated scanners."

    lines = [f"Automated scanners detected {len(issues)} open issue(s)."]
    if len(issues) > max_issues:
        lines.append(f"Showing top {max_issues} by priority score:")
        issues = issues[:max_issues]

    for idx, issue in enumerate(issues, 1):
        evidence = json.loads(issue.evidence_json) if issue.evidence_json else {}
        lines.append(
            f"\n[{idx}] {issue.issue_type.upper()} | {issue.severity} | score={issue.score:.2f}"
        )
        lines.append(f"    target_type={issue.target_type}  target_id={issue.target_id}")
        lines.append(f"    {issue.reason}")
        if evidence:
            lines.append(f"    evidence: {json.dumps(evidence)}")

    return "\n".join(lines)
