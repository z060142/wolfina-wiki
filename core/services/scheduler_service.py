"""Dynamic maintenance scheduler.

The scheduler runs a background asyncio loop that periodically invokes the
orchestrator → specialist agent maintenance pipeline.

Interval logic:
  - Tracks timestamps of recent conversation flush events in a rolling deque
    (look-back window = scheduler_rate_window_hours hours).
  - Maps flush rate to interval via linear interpolation:
      rate == 0                     → max_interval
      rate >= scheduler_target_rate → min_interval
      in between                    → linear blend
  - Clamped to [min_interval, max_interval].

This means: the more conversations the wiki is receiving, the more frequently
the maintenance agents run to keep knowledge organised.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from core.db.base import AsyncSessionLocal
from core.settings import settings

logger = logging.getLogger(__name__)


class DynamicScheduler:
    """Singleton scheduler started during FastAPI lifespan."""

    def __init__(self) -> None:
        self._flush_times: deque[datetime] = deque()
        self._task: asyncio.Task | None = None
        self._running = False

    # ── public interface ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="wiki-scheduler")
        logger.info(
            "Scheduler started — min=%ds max=%ds window=%dh",
            settings.scheduler_min_interval_seconds,
            settings.scheduler_max_interval_seconds,
            settings.scheduler_rate_window_hours,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler stopped.")

    def record_flush(self) -> None:
        """Call this each time a conversation window flush completes."""
        now = datetime.now(timezone.utc)
        self._flush_times.append(now)
        self._prune_old()
        logger.debug(
            "Flush recorded — rate=%d flushes in last %dh → next interval ~%ds",
            len(self._flush_times),
            settings.scheduler_rate_window_hours,
            self._calculate_interval(),
        )

    def current_interval(self) -> int:
        return self._calculate_interval()

    def flush_rate(self) -> int:
        self._prune_old()
        return len(self._flush_times)

    # ── internals ─────────────────────────────────────────────────────────────

    def _prune_old(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.scheduler_rate_window_hours)
        while self._flush_times and self._flush_times[0] < cutoff:
            self._flush_times.popleft()

    def _calculate_interval(self) -> int:
        self._prune_old()
        rate = len(self._flush_times)
        target = max(settings.scheduler_target_rate, 1)
        ratio = min(rate / target, 1.0)
        span = settings.scheduler_max_interval_seconds - settings.scheduler_min_interval_seconds
        interval = settings.scheduler_max_interval_seconds - int(ratio * span)
        return max(
            settings.scheduler_min_interval_seconds,
            min(settings.scheduler_max_interval_seconds, interval),
        )

    async def _loop(self) -> None:
        while self._running:
            interval = self._calculate_interval()
            logger.debug("Scheduler sleeping %ds", interval)
            await asyncio.sleep(interval)
            if not self._running:
                break
            await self._run_maintenance()

    async def _run_maintenance(self) -> None:
        logger.info(
            "Scheduler: running maintenance — flush_rate=%d/last-%dh",
            self.flush_rate(),
            settings.scheduler_rate_window_hours,
        )
        from core.services.agent_service import run_maintenance_pipeline

        async with AsyncSessionLocal() as db:
            try:
                await run_maintenance_pipeline(db)
            except Exception:
                logger.exception("Scheduler: maintenance pipeline error")


# Module-level singleton — imported by conversation_service and app.py
scheduler = DynamicScheduler()
