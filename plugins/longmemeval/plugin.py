"""LongMemEval plugin metadata.

This plugin currently exposes a CLI workflow (longmemeval_demo.py) and can be
expanded with HTTP routes if needed later.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from core.events.event_bus import EventBus
from plugins.base_plugin import BasePlugin

logger = logging.getLogger(__name__)


class LongMemEvalPlugin(BasePlugin):
    @property
    def name(self) -> str:
        return "longmemeval"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def capabilities(self) -> list[str]:
        return [
            "cli:longmemeval-demo",
            "dataset:longmemeval-install",
            "eval:longmemeval-score",
            "read:conversation-window-status",
        ]

    async def on_load(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        self._wiki_base_url = os.environ.get("LONGMEMEVAL_WIKI_BASE_URL", "http://localhost:8000").rstrip("/")
        self._http = httpx.AsyncClient(timeout=30.0)
        logger.info("[LongMemEvalPlugin] Loaded.")

    async def on_unload(self) -> None:
        await self._http.aclose()
        logger.info("[LongMemEvalPlugin] Unloaded.")

    async def get_window_status(self, window_id: str) -> dict[str, Any]:
        """Read one conversation window status from wiki API."""
        resp = await self._http.get(
            f"{self._wiki_base_url}/conversations/windows/{window_id}",
            headers={"X-Agent-ID": "longmemeval-plugin"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "window_id": data.get("id"),
            "status": data.get("status"),
            "message_count": data.get("message_count"),
            "total_char_count": data.get("total_char_count"),
        }

    async def list_flushing_windows(self, limit: int = 50) -> list[dict[str, Any]]:
        """Read current flushing windows (for throttling/observability)."""
        resp = await self._http.get(
            f"{self._wiki_base_url}/conversations/windows",
            params={"status": "flushing", "limit": limit},
            headers={"X-Agent-ID": "longmemeval-plugin"},
        )
        resp.raise_for_status()
        windows = resp.json()
        if not isinstance(windows, list):
            return []
        return windows
