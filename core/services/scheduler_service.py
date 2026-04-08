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
        self._janitor_task: asyncio.Task | None = None
        self._running = False
        self._nudge_event: asyncio.Event = asyncio.Event()
        self._maintenance_running: bool = False

    # ── public interface ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="wiki-scheduler")
        self._janitor_task = asyncio.create_task(self._janitor_loop(), name="wiki-janitor")
        logger.info(
            "Scheduler started — min=%ds max=%ds window=%dh | janitor=%ds",
            settings.scheduler_min_interval_seconds,
            settings.scheduler_max_interval_seconds,
            settings.scheduler_rate_window_hours,
            settings.janitor_interval_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        for t in (self._task, self._janitor_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._janitor_task = None
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

    def nudge(self) -> None:
        """Wake the maintenance loop early (called by janitor when stale tasks are found)."""
        if not self._maintenance_running:
            logger.info("Scheduler: nudged by janitor — waking maintenance loop early")
            self._nudge_event.set()

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
            self._nudge_event.clear()
            logger.debug("Scheduler sleeping %ds (or until nudged)", interval)
            try:
                await asyncio.wait_for(self._nudge_event.wait(), timeout=interval)
                logger.info("Scheduler: woken early by nudge")
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            await self._run_maintenance()

    async def _run_maintenance(self) -> None:
        self._maintenance_running = True
        logger.info(
            "Scheduler: running maintenance — flush_rate=%d/last-%dh",
            self.flush_rate(),
            settings.scheduler_rate_window_hours,
        )
        from core.debug.event_stream import debug_stream
        debug_stream.emit(
            "scheduler_tick",
            flush_rate=self.flush_rate(),
            current_interval=self._calculate_interval(),
        )
        from core.services.agent_service import run_ingest_pipeline, run_maintenance_pipeline

        from core.services.conversation_service import flush_pending_windows
        try:
            triggered = await flush_pending_windows()
            if triggered:
                logger.info("Scheduler: flushed %d pending window(s)", triggered)
        except Exception:
            logger.exception("Scheduler: flush scan error")

        async with AsyncSessionLocal() as db:
            try:
                await run_maintenance_pipeline(db)
            except Exception:
                logger.exception("Scheduler: maintenance pipeline error")

        # Run ingest pipeline only when FILE_READ_ALLOWED_DIRS is configured
        if settings.file_read_allowed_dirs.strip():
            async with AsyncSessionLocal() as db:
                try:
                    await run_ingest_pipeline(db)
                except Exception:
                    logger.exception("Scheduler: ingest pipeline error")

        self._maintenance_running = False


    async def _janitor_loop(self) -> None:
        """Independent patrol loop — runs every janitor_interval_seconds regardless
        of the maintenance pipeline cadence."""
        # Stagger startup so the janitor doesn't fire at the same moment as the
        # first maintenance cycle.
        await asyncio.sleep(settings.janitor_interval_seconds // 2)
        while self._running:
            try:
                from core.services.janitor_service import run_janitor_once
                await run_janitor_once()
            except Exception:
                logger.exception("Janitor: patrol error")
            logger.debug("Janitor sleeping %ds", settings.janitor_interval_seconds)
            await asyncio.sleep(settings.janitor_interval_seconds)


# Module-level singleton — imported by conversation_service and app.py
scheduler = DynamicScheduler()
