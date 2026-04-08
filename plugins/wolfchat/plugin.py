"""Wolf Chat bridge plugin.

Bridges Wolfina Wiki with the Wolf Chat bot system:
  1. Query endpoint  — receives a username, runs quick_query, returns a cached summary
                       for Wolf Chat to use as memory/context.
  2. Ingest endpoint — receives a finished conversation log from Wolf Chat, normalises
                       it, and submits it to a ConversationWindow so the automated
                       wiki pipeline can extract knowledge from it.

HTTP endpoints are registered in plugins/wolfchat/router.py and manually included
in api/app.py.  This plugin class handles event-bus concerns and shared state
(the query cache).

Wolf Chat integration points (to be wired up on the Wolf Chat side):
  - GET  /wolfchat/user/{username}          → query user memory
  - POST /wolfchat/conversation             → push conversation log
"""

import json
import logging
from pathlib import Path

from core.events.event_bus import EventBus
from plugins.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


class WolfChatPlugin(BasePlugin):
    """Wolf Chat ↔ Wolfina Wiki bridge."""

    # Shared in-process query cache: {username_lower: (timestamp, summary, sources)}
    _query_cache: dict = {}

    @property
    def name(self) -> str:
        return "wolfchat"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def capabilities(self) -> list[str]:
        return [
            "api:wolfchat/user",
            "api:wolfchat/conversation",
            "query:quick_query",
            "ingest:conversation_window",
        ]

    async def on_load(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        cfg = load_config()
        logger.info(
            "[WolfChatPlugin] Loaded. bot_display_name=%s, cache_ttl=%ss",
            cfg.get("bot_display_name"),
            cfg.get("query_cache_ttl_seconds"),
        )

    async def on_unload(self) -> None:
        WolfChatPlugin._query_cache.clear()
        logger.info("[WolfChatPlugin] Unloaded, cache cleared.")
