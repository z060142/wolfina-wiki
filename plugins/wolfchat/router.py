"""FastAPI router for the Wolf Chat bridge plugin.

Endpoints:
  GET  /wolfchat/user/{username}
      Query Wolfina Wiki for a user's profile/history and return a summary.
      Results are cached per-username for `query_cache_ttl_seconds` (default 10 min).

  POST /wolfchat/conversation
      Receive a finished Wolf Chat conversation log, normalise it, and push it
      into a ConversationWindow for automated wiki processing.

  [RESERVED] POST /wolfchat/webhook  — future Wolf Chat → Wiki push hook
  [RESERVED] GET  /wolfchat/status   — health / diagnostics for Wolf Chat side
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.schemas.conversation import MessageAdd
from core.services import conversation_service
from plugins.wolfchat.plugin import WolfChatPlugin, load_config

router = APIRouter(prefix="/wolfchat", tags=["wolfchat"])
logger = logging.getLogger(__name__)


# ── Pydantic models ────────────────────────────────────────────────────────────

class UserQueryResponse(BaseModel):
    username: str
    summary: str
    sources: list[str]
    cached: bool
    cache_age_seconds: float | None = None


class DirectQueryRequest(BaseModel):
    query: str = Field(..., description="Free-form query string (e.g. a username, topic, or question).")
    summary_instruction: str | None = Field(None, description="Custom instruction for the LLM summariser. Overrides config default.")
    max_words: int = Field(500, description="Maximum words in the returned summary.")


class DirectQueryResponse(BaseModel):
    query: str
    summary: str
    sources: list[str]


class ConversationTurn(BaseModel):
    """One exchange: a user message plus the bot's thoughts and dialogue."""
    timestamp: str
    username: str
    user_message: str
    bot_thoughts: str
    bot_dialogue: str


class ConversationLogRequest(BaseModel):
    """
    Raw conversation log from Wolf Chat.

    `raw_log` is the full multi-line text in the format:
        [YYYY-MM-DD HH:MM:SS] User (Name): ...
        [YYYY-MM-DD HH:MM:SS] Bot (AnyName) Thoughts: ...
        [YYYY-MM-DD HH:MM:SS] Bot (AnyName) Dialogue: ...

    Optionally supply `turns` (pre-parsed) instead of `raw_log`.
    If both are provided, `turns` takes precedence.
    """
    raw_log: str | None = Field(None, description="Raw multi-line conversation log text.")
    turns: list[ConversationTurn] | None = Field(None, description="Pre-parsed turns (overrides raw_log).")
    session_id: str | None = Field(None, description="Optional session identifier from Wolf Chat.")


class ConversationIngestResponse(BaseModel):
    window_id: str
    messages_added: int
    flush_triggered: bool


# ── Log parser ─────────────────────────────────────────────────────────────────

# Matches: [2026-01-30 00:56:13] User (SherefoxUwU): some text
_USER_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+User\s+\((?P<name>[^)]+)\):\s*(?P<text>.+)$"
)
# Matches: [2026-01-30 00:56:13] Bot (Wolfhart) Thoughts: some text
_BOT_THOUGHT_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+Bot\s+\([^)]+\)\s+Thoughts:\s*(?P<text>.+)$"
)
# Matches: [2026-01-30 00:56:13] Bot (Wolfhart) Dialogue: some text
_BOT_DIALOGUE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+Bot\s+\([^)]+\)\s+Dialogue:\s*(?P<text>.+)$"
)


def parse_raw_log(raw: str) -> list[ConversationTurn]:
    """Parse raw Wolf Chat log text into ConversationTurn objects.

    Expects lines grouped in triplets: user → bot_thoughts → bot_dialogue.
    Lines that don't match any pattern are skipped with a warning.
    """
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    turns: list[ConversationTurn] = []

    i = 0
    while i < len(lines):
        um = _USER_RE.match(lines[i])
        if not um:
            logger.warning("[WolfChat] Skipping unrecognised line: %s", lines[i])
            i += 1
            continue

        ts = um.group("ts")
        username = um.group("name")
        user_text = um.group("text")

        thoughts = ""
        dialogue = ""

        if i + 1 < len(lines):
            tm = _BOT_THOUGHT_RE.match(lines[i + 1])
            if tm:
                thoughts = tm.group("text")
                i += 1

        if i + 1 < len(lines):
            dm = _BOT_DIALOGUE_RE.match(lines[i + 1])
            if dm:
                dialogue = dm.group("text")
                i += 1

        turns.append(ConversationTurn(
            timestamp=ts,
            username=username,
            user_message=user_text,
            bot_thoughts=thoughts,
            bot_dialogue=dialogue,
        ))
        i += 1

    return turns


# ── GET /wolfchat/user/{username} ──────────────────────────────────────────────

@router.get("/user/{username}", response_model=UserQueryResponse)
async def query_user(
    username: str,
    db: AsyncSession = Depends(get_db),
) -> UserQueryResponse:
    """Return a Wolfina Wiki summary for the given username.

    Results are cached per-username for `query_cache_ttl_seconds` (default 600 s).
    Cache is in-process (resets on server restart) — sufficient for a low-traffic bot.
    """
    cfg = load_config()
    ttl: float = float(cfg.get("query_cache_ttl_seconds", 600))
    instruction: str = cfg.get("query_summary_instruction", "")
    max_words: int = int(cfg.get("query_max_words", 500))

    cache_key = username.lower()
    now = time.monotonic()
    cached_entry = WolfChatPlugin._query_cache.get(cache_key)

    if cached_entry:
        cached_at, summary, sources = cached_entry
        age = now - cached_at
        if age < ttl:
            logger.debug("[WolfChat] Cache hit for user '%s' (age %.1fs)", username, age)
            return UserQueryResponse(
                username=username,
                summary=summary,
                sources=sources,
                cached=True,
                cache_age_seconds=round(age, 1),
            )

    # Cache miss — run quick_query
    logger.info("[WolfChat] Cache miss for user '%s', running quick_query.", username)
    from core.tools.handlers import _quick_query

    result = await _quick_query(
        {
            "query": username,
            "summary_instruction": instruction,
            "max_words": max_words,
        },
        db,
    )

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    summary: str = result["summary"]
    sources: list[str] = result.get("sources", [])

    WolfChatPlugin._query_cache[cache_key] = (now, summary, sources)

    return UserQueryResponse(
        username=username,
        summary=summary,
        sources=sources,
        cached=False,
        cache_age_seconds=None,
    )


# ── POST /wolfchat/query ───────────────────────────────────────────────────────

@router.post("/query", response_model=DirectQueryResponse)
async def direct_query(
    body: DirectQueryRequest,
    db: AsyncSession = Depends(get_db),
) -> DirectQueryResponse:
    """Run a live quick_query without caching. Intended for Wolf Chat's LLM to call
    autonomously when it needs up-to-date wiki information mid-conversation.

    Unlike GET /wolfchat/user/{username}, results are never cached and the query
    string is not limited to a username — any topic or free-form question works.
    """
    cfg = load_config()
    instruction: str = body.summary_instruction or cfg.get("query_summary_instruction", "")

    from core.tools.handlers import _quick_query

    result = await _quick_query(
        {
            "query": body.query,
            "summary_instruction": instruction,
            "max_words": body.max_words,
        },
        db,
    )

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return DirectQueryResponse(
        query=body.query,
        summary=result["summary"],
        sources=result.get("sources", []),
    )


# ── POST /wolfchat/conversation ────────────────────────────────────────────────

@router.post("/conversation", response_model=ConversationIngestResponse, status_code=201)
async def ingest_conversation(
    body: ConversationLogRequest,
    db: AsyncSession = Depends(get_db),
) -> ConversationIngestResponse:
    """Receive a finished Wolf Chat conversation and push it into the wiki pipeline.

    The bot is relabelled to the display name in config.json.  The user is
    labelled as a player.  Each turn becomes two ConversationWindow messages
    (user + assistant) so the standard flush pipeline can process them.
    """
    cfg = load_config()
    bot_name: str = cfg.get("bot_display_name", "Wolfina Vally")
    bot_label: str = cfg.get("bot_role_label", "chatbot")
    player_label: str = cfg.get("player_role_label", "player")
    source_id: str = cfg.get("conversation_window_source_id", "wolfchat")
    character_analysis_instruction: str = cfg.get("flush_character_analysis_instruction", "")

    # Resolve turns
    turns: list[ConversationTurn]
    if body.turns:
        turns = body.turns
    elif body.raw_log:
        turns = parse_raw_log(body.raw_log)
    else:
        raise HTTPException(status_code=422, detail="Provide either `raw_log` or `turns`.")

    if not turns:
        raise HTTPException(status_code=422, detail="No conversation turns could be parsed.")

    # Reuse an existing active window for the same source, or create one if none exists.
    # This lets messages accumulate across multiple conversation batches until the
    # wiki scheduler's natural flush conditions are met.
    effective_source = f"{source_id}:{body.session_id}" if body.session_id else source_id
    from core.models.conversation import ConversationWindow, WindowStatus
    existing = await db.scalar(
        select(ConversationWindow)
        .where(ConversationWindow.external_source_id == effective_source)
        .where(ConversationWindow.status == WindowStatus.active)
        .order_by(ConversationWindow.created_at.desc())
        .limit(1)
    )
    if existing:
        window_id = existing.id
        logger.debug("[WolfChat] Reusing window %s for source '%s'", window_id, effective_source)
    else:
        window = await conversation_service.create_window(db, external_source_id=effective_source)
        window_id = window.id
        await db.commit()
        logger.debug("[WolfChat] Created new window %s for source '%s'", window_id, effective_source)

    messages_added = 0
    flush_triggered = False

    for turn in turns:
        # ── User message ──
        user_content = (
            f"[{player_label}: {turn.username}] [{turn.timestamp}]\n"
            f"{turn.user_message}"
        )
        _, _ = await conversation_service.add_message(
            db,
            window_id,
            MessageAdd(role="user", content=user_content),
        )
        await db.commit()
        messages_added += 1

        # ── Assistant message (bot thoughts + dialogue) ──
        parts: list[str] = [f"[{bot_label}: {bot_name}] [{turn.timestamp}]"]
        if turn.bot_thoughts:
            parts.append(f"[Thoughts] {turn.bot_thoughts}")
        if turn.bot_dialogue:
            parts.append(f"[Dialogue] {turn.bot_dialogue}")
        assistant_content = "\n".join(parts)

        _, should_flush = await conversation_service.add_message(
            db,
            window_id,
            MessageAdd(role="assistant", content=assistant_content),
        )
        await db.commit()
        messages_added += 1

        if should_flush:
            await conversation_service.trigger_flush(
                window_id,
                extra_proposer_instructions=character_analysis_instruction,
            )
            flush_triggered = True

    logger.info(
        "[WolfChat] Ingested %d turns into window %s (flush_triggered=%s)",
        len(turns), window_id, flush_triggered,
    )

    return ConversationIngestResponse(
        window_id=window_id,
        messages_added=messages_added,
        flush_triggered=flush_triggered,
    )


# ── Reserved stubs ─────────────────────────────────────────────────────────────

@router.post("/webhook", status_code=501)
async def webhook_stub() -> dict:
    """[RESERVED] Future real-time webhook from Wolf Chat. Not yet implemented."""
    return {"detail": "Not implemented — reserved for Wolf Chat webhook integration."}


@router.get("/status")
async def status() -> dict:
    """Diagnostics: cache size and config snapshot."""
    cfg = load_config()
    cache = WolfChatPlugin._query_cache
    now = time.monotonic()
    ttl = float(cfg.get("query_cache_ttl_seconds", 600))
    cached_users = [
        {"username": k, "age_seconds": round(now - v[0], 1)}
        for k, v in cache.items()
        if (now - v[0]) < ttl
    ]
    return {
        "plugin": "wolfchat",
        "version": "0.1.0",
        "config": {k: v for k, v in cfg.items() if not k.startswith("_")},
        "active_cache_entries": cached_users,
    }
