"""Debug event broadcaster.

A module-level singleton (debug_stream) accepts emit() calls from anywhere
in the codebase and fans them out to all connected SSE clients.

Usage:
    from core.debug.event_stream import debug_stream
    debug_stream.emit("agent_tool_call", agent_type="proposer", tool="propose_new_page")
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any


class DebugEventStream:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[str]] = []

    # ── client lifecycle ──────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[str]:
        """Register a new SSE client; returns its exclusive queue."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        """Remove a disconnected client's queue."""
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    # ── broadcast ─────────────────────────────────────────────────────────────

    def emit(self, event_type: str, **data: Any) -> None:
        """Broadcast an event to every connected SSE client (non-blocking).

        Queues that are full (slow clients) are silently dropped.
        """
        payload: dict[str, Any] = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **data,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        dead: list[asyncio.Queue[str]] = []
        for q in self._queues:
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self._queues.remove(q)
            except ValueError:
                pass


# Module-level singleton imported by all services.
debug_stream = DebugEventStream()
