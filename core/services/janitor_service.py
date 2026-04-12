"""Task Janitor — pipeline supervisor for AgentTask health.

Runs on a short independent interval (default 2 min) and focuses on one question:
  "Is the work getting done?"

It does NOT wait for the maintenance pipeline. It acts immediately when it finds:

  1. Crashed tasks   — status=running but started > janitor_running_timeout_minutes ago
                       → reset to pending so the next maintenance cycle picks them up.

  2. Failed tasks    — retry_count < janitor_max_task_retries
                       → reset to pending immediately for a quick retry.

  3. Duplicate tasks — multiple pending tasks with same agent_type + instruction
                       → keep the newest, delete the rest.

  4. Stale pending   — pending tasks older than janitor_pending_timeout_minutes
                       → nudge the scheduler to run maintenance immediately.

  5. Pipeline gaps   — pending proposals with no pending reviewer task
                       → create a reviewer task on the spot.
                     — approved proposals with no pending executor task
                       → create an executor task on the spot.

  6. Old records     — done/failed tasks older than janitor_task_retention_days
                       → delete to prevent DB bloat.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.base import AsyncSessionLocal
from core.models.conversation import AgentTask, TaskStatus
from core.models.proposal import EditProposal, ProposalStatus
from core.settings import settings

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _instruction_fingerprint(agent_type: str, instruction: str) -> str:
    """Short hash of (agent_type, instruction) for dedup comparison."""
    raw = f"{agent_type}::{instruction.strip().lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class JanitorReport:
    crashed_reset: int = 0          # running → pending (agent died)
    failed_retried: int = 0         # failed → pending (retry)
    failed_abandoned: int = 0       # failed, max retries exceeded — left alone
    duplicates_removed: int = 0     # duplicate pending tasks deleted
    stale_pending: int = 0          # pending too long → scheduler nudged
    reviewer_tasks_created: int = 0 # new reviewer tasks for orphaned proposals
    executor_tasks_created: int = 0 # new executor tasks for approved proposals
    old_records_deleted: int = 0    # stale done/failed tasks purged
    errors: list[str] = field(default_factory=list)


async def run_task_janitor(db: AsyncSession) -> JanitorReport:
    """Main janitor pass. Returns a report of all actions taken."""
    report = JanitorReport()
    now = _now()

    # ── 1. Crashed tasks: running → pending ───────────────────────────────────
    running_cutoff = now - timedelta(minutes=settings.janitor_running_timeout_minutes)
    crashed = (await db.scalars(
        select(AgentTask).where(
            AgentTask.status == TaskStatus.running,
            AgentTask.started_at < running_cutoff,
        )
    )).all()

    for task in crashed:
        logger.warning(
            "Janitor: crashed task %s (%s) started=%s — resetting to pending (retry %d)",
            task.id, task.agent_type, task.started_at, task.retry_count + 1,
        )
        task.status = TaskStatus.pending
        task.started_at = None
        task.retry_count += 1
        task.error_message = f"[janitor] reset from running after timeout (attempt {task.retry_count})"
        report.crashed_reset += 1

    # ── 2. Failed tasks: retry or abandon ─────────────────────────────────────
    failed_tasks = (await db.scalars(
        select(AgentTask).where(AgentTask.status == TaskStatus.failed)
    )).all()

    for task in failed_tasks:
        if task.retry_count < settings.janitor_max_task_retries:
            logger.info(
                "Janitor: retrying failed task %s (%s) retry=%d/%d",
                task.id, task.agent_type, task.retry_count + 1, settings.janitor_max_task_retries,
            )
            task.status = TaskStatus.pending
            task.retry_count += 1
            task.error_message = f"[janitor] retry {task.retry_count}/{settings.janitor_max_task_retries}"
            task.started_at = None
            task.completed_at = None
            report.failed_retried += 1
        else:
            report.failed_abandoned += 1

    # ── 3. Duplicate pending tasks: keep newest, delete older ─────────────────
    pending_tasks = (await db.scalars(
        select(AgentTask).where(AgentTask.status == TaskStatus.pending)
        .order_by(AgentTask.created_at.desc())
    )).all()

    seen_fingerprints: dict[str, str] = {}  # fingerprint → kept task id
    for task in pending_tasks:
        fp = _instruction_fingerprint(task.agent_type, task.instruction)
        if fp in seen_fingerprints:
            logger.info(
                "Janitor: dedup — removing task %s (%s), duplicate of %s",
                task.id, task.agent_type, seen_fingerprints[fp],
            )
            await db.delete(task)
            report.duplicates_removed += 1
        else:
            seen_fingerprints[fp] = task.id

    await db.flush()  # apply deletes before the gap checks below

    # ── 4. Stale pending tasks: abandon very old ones, nudge for the rest ────────
    abandon_cutoff = now - timedelta(hours=settings.janitor_pending_abandon_hours)
    stale_cutoff = now - timedelta(minutes=settings.janitor_pending_timeout_minutes)

    stale_pending_tasks = (await db.scalars(
        select(AgentTask).where(
            AgentTask.status == TaskStatus.pending,
            AgentTask.created_at < stale_cutoff,
        )
    )).all()

    nudge_needed = False
    for task in stale_pending_tasks:
        if task.created_at.replace(tzinfo=timezone.utc) < abandon_cutoff:
            # Been pending for too long — give up rather than nudging forever.
            task.status = TaskStatus.failed
            task.completed_at = now
            task.error_message = (
                "[janitor] abandoned after exceeding pending timeout "
                f"({settings.janitor_pending_abandon_hours}h)"
            )
            logger.warning(
                "Janitor: abandoning task %s (%s) — pending since %s",
                task.id, task.agent_type, task.created_at,
            )
        else:
            nudge_needed = True

    if nudge_needed:
        still_stale = [t for t in stale_pending_tasks if t.status == TaskStatus.pending]
        by_type: dict[str, int] = {}
        for t in still_stale:
            by_type[t.agent_type] = by_type.get(t.agent_type, 0) + 1
        report.stale_pending = len(still_stale)
        logger.warning(
            "Janitor: %d stale pending task(s) found %s — nudging maintenance",
            len(still_stale),
            dict(by_type),
        )
        from core.services.scheduler_service import scheduler
        scheduler.nudge()

    # ── 5. Pipeline gap: pending proposals without a reviewer task ────────────
    pending_proposals = (await db.scalars(
        select(EditProposal).where(EditProposal.status == ProposalStatus.pending)
    )).all()

    if pending_proposals:
        # Check if there is already a pending reviewer task
        existing_reviewer = await db.scalar(
            select(AgentTask).where(
                AgentTask.agent_type == "reviewer",
                AgentTask.status == TaskStatus.pending,
            )
        )
        if existing_reviewer is None:
            proposal_ids = [p.id for p in pending_proposals]
            task = AgentTask(
                agent_type="reviewer",
                instruction=(
                    f"Review {len(proposal_ids)} pending proposal(s). "
                    f"Use list_proposals with status='pending' to find them, "
                    f"then review each one with review_proposal."
                ),
                context_json=__import__("json").dumps({"proposal_ids": proposal_ids}),
                batch_id=None,
                retry_count=0,
            )
            db.add(task)
            logger.info(
                "Janitor: created reviewer task for %d orphaned pending proposal(s)",
                len(proposal_ids),
            )
            report.reviewer_tasks_created += 1

    # ── 5. Pipeline gap: approved proposals without an executor task ──────────
    approved_proposals = (await db.scalars(
        select(EditProposal).where(EditProposal.status == ProposalStatus.approved)
    )).all()

    if approved_proposals:
        existing_executor = await db.scalar(
            select(AgentTask).where(
                AgentTask.agent_type == "executor",
                AgentTask.status == TaskStatus.pending,
            )
        )
        if existing_executor is None:
            proposal_ids = [p.id for p in approved_proposals]
            task = AgentTask(
                agent_type="executor",
                instruction=(
                    f"Apply {len(proposal_ids)} approved proposal(s). "
                    f"Use list_proposals with status='approved' to find them, "
                    f"then apply each one with apply_proposal."
                ),
                context_json=__import__("json").dumps({"proposal_ids": proposal_ids}),
                batch_id=None,
                retry_count=0,
            )
            db.add(task)
            logger.info(
                "Janitor: created executor task for %d orphaned approved proposal(s)",
                len(proposal_ids),
            )
            report.executor_tasks_created += 1

    # ── 6. Old record purge: done/failed tasks older than retention window ────
    retention_cutoff = now - timedelta(days=settings.janitor_task_retention_days)
    deleted = await db.execute(
        delete(AgentTask)
        .where(
            AgentTask.status.in_([TaskStatus.done, TaskStatus.failed]),
            AgentTask.completed_at < retention_cutoff,
        )
        .execution_options(synchronize_session=False)
    )
    report.old_records_deleted = deleted.rowcount

    await db.commit()

    # ── Emit debug event ──────────────────────────────────────────────────────
    from core.debug.event_stream import debug_stream
    debug_stream.emit(
        "janitor_pass",
        crashed_reset=report.crashed_reset,
        failed_retried=report.failed_retried,
        failed_abandoned=report.failed_abandoned,
        duplicates_removed=report.duplicates_removed,
        stale_pending=report.stale_pending,
        reviewer_tasks_created=report.reviewer_tasks_created,
        executor_tasks_created=report.executor_tasks_created,
        old_records_deleted=report.old_records_deleted,
    )

    if any([
        report.crashed_reset, report.failed_retried, report.duplicates_removed,
        report.stale_pending, report.reviewer_tasks_created, report.executor_tasks_created,
    ]):
        logger.info(
            "Janitor pass complete — crashed=%d retried=%d dedup=%d "
            "stale_nudged=%d reviewer_tasks=%d executor_tasks=%d purged=%d",
            report.crashed_reset, report.failed_retried, report.duplicates_removed,
            report.stale_pending, report.reviewer_tasks_created, report.executor_tasks_created,
            report.old_records_deleted,
        )

    return report


async def run_janitor_once() -> JanitorReport:
    """Entry point for the scheduler loop — manages its own DB session."""
    async with AsyncSessionLocal() as db:
        return await run_task_janitor(db)
