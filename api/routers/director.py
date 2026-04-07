"""Director API router.

POST /director/sessions              — create a new director session
GET  /director/sessions              — list sessions
GET  /director/sessions/{id}         — get session (messages + todo)
DELETE /director/sessions/{id}       — delete session
POST /director/sessions/{id}/chat    — send a message, returns SSE stream of events
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.exceptions import NotFound

router = APIRouter(prefix="/director", tags=["director"])
logger = logging.getLogger(__name__)


class CreateSessionRequest(BaseModel):
    title: str = "New Session"


class ChatRequest(BaseModel):
    message: str


# ── helpers ───────────────────────────────────────────────────────────────────

def _session_out(s) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "todo_list": json.loads(s.todo_list or "[]"),
        "message_count": len(json.loads(s.messages or "[]")),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from core.services.director_service import create_session
    session = await create_session(db, title=body.title)
    return _session_out(session)


@router.get("/sessions")
async def list_sessions(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from core.services.director_service import list_sessions
    sessions = await list_sessions(db, limit=limit)
    return {"sessions": [_session_out(s) for s in sessions]}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from core.services.director_service import get_session
    session = await get_session(db, session_id)
    if session is None:
        raise NotFound(f"Director session {session_id!r} not found.")
    out = _session_out(session)
    out["messages"] = json.loads(session.messages or "[]")
    return out


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    from core.services.director_service import delete_session
    deleted = await delete_session(db, session_id)
    if not deleted:
        raise NotFound(f"Director session {session_id!r} not found.")


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a message to the director and stream back SSE events.

    Event format:
        data: {"type": "thinking", "iteration": 0}
        data: {"type": "tool_call", "tool": "...", "args": {...}}
        data: {"type": "tool_result", "tool": "...", "ok": true, "preview": "..."}
        data: {"type": "delegate", "agent_type": "...", "instruction": "..."}
        data: {"type": "pipeline", "pipeline_type": "..."}
        data: {"type": "reply", "text": "..."}
        data: {"type": "error", "message": "..."}
        data: {"type": "done"}
    """
    from core.services.director_service import get_session, run_director_turn

    session = await get_session(db, session_id)
    if session is None:
        raise NotFound(f"Director session {session_id!r} not found.")

    # Collect events in a thread-safe queue so the SSE generator can yield them.
    q: asyncio.Queue[dict | None] = asyncio.Queue()

    def on_event(evt: dict) -> None:
        q.put_nowait(evt)

    async def _run() -> None:
        try:
            await run_director_turn(db, session, body.message, on_event=on_event)
        except Exception as exc:
            logger.exception("Director turn error")
            q.put_nowait({"type": "error", "message": str(exc)})
        finally:
            q.put_nowait(None)  # sentinel

    async def _sse_stream():
        task = asyncio.ensure_future(_run())
        try:
            while True:
                evt = await q.get()
                if evt is None:
                    yield "data: " + json.dumps({"type": "done"}) + "\n\n"
                    break
                yield "data: " + json.dumps(evt, default=str) + "\n\n"
        finally:
            task.cancel()

    return StreamingResponse(_sse_stream(), media_type="text/event-stream")
