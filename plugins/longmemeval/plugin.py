"""LongMemEval plugin metadata.

This plugin currently exposes a CLI workflow (longmemeval_demo.py) and can be
expanded with HTTP routes if needed later.
"""

from __future__ import annotations

import logging

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
        ]

    async def on_load(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        logger.info("[LongMemEvalPlugin] Loaded.")

    async def on_unload(self) -> None:
        logger.info("[LongMemEvalPlugin] Unloaded.")
