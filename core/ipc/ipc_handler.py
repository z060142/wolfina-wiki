"""JSON-lines stdin/stdout IPC handler.

Run as a subprocess:
    python -m core.ipc

The parent process sends one JSON object per line to stdin.
The IPC handler writes one JSON object per line to stdout as a response.

Supported commands
------------------
{"cmd": "create_window", "external_source_id": "..."}
    → {"ok": true, "window_id": "..."}

{"cmd": "add_message", "window_id": "...", "role": "user", "content": "..."}
    → {"ok": true, "message_id": "...", "sequence_no": 0, "flush_triggered": false}

{"cmd": "get_window", "window_id": "..."}
    → {"ok": true, "window": {...}}

{"cmd": "list_messages", "window_id": "...", "include_processed": true}
    → {"ok": true, "messages": [...]}

{"cmd": "flush", "window_id": "..."}
    → {"ok": true}

{"cmd": "list_windows", "status": null}
    → {"ok": true, "windows": [...]}

{"cmd": "list_tasks", "status": "pending", "agent_type": null}
    → {"ok": true, "tasks": [...]}

{"cmd": "scheduler_status"}
    → {"ok": true, "flush_rate": 3, "current_interval_seconds": 600}

{"cmd": "ping"}
    → {"ok": true, "pong": true}

{"cmd": "shutdown"}
    → {"ok": true} then the process exits cleanly

On error:
    → {"ok": false, "error": "description"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


# ── response helpers ──────────────────────────────────────────────────────────

def _ok(**kwargs: Any) -> str:
    return json.dumps({"ok": True, **kwargs}, ensure_ascii=False, default=str)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


# ── command handlers ──────────────────────────────────────────────────────────

async def _handle(cmd: dict) -> str:
    from core.db.base import AsyncSessionLocal
    from core.schemas.conversation import MessageAdd
    from core.services import conversation_service
    from core.services.scheduler_service import scheduler

    op = cmd.get("cmd", "")

    if op == "ping":
        return _ok(pong=True)

    if op == "scheduler_status":
        return _ok(
            flush_rate=scheduler.flush_rate(),
            current_interval_seconds=scheduler.current_interval(),
        )

    if op == "create_window":
        async with AsyncSessionLocal() as db:
            async with db.begin():
                window = await conversation_service.create_window(
                    db, cmd.get("external_source_id", "")
                )
        return _ok(window_id=window.id)

    if op == "add_message":
        async with AsyncSessionLocal() as db:
            async with db.begin():
                data = MessageAdd(role=cmd["role"], content=cmd["content"])
                msg, should_flush = await conversation_service.add_message(
                    db, cmd["window_id"], data
                )
        if should_flush:
            await conversation_service.trigger_flush(cmd["window_id"])
        return _ok(
            message_id=msg.id,
            sequence_no=msg.sequence_no,
            flush_triggered=should_flush,
        )

    if op == "get_window":
        async with AsyncSessionLocal() as db:
            async with db.begin():
                window = await conversation_service.get_window(db, cmd["window_id"])
        return _ok(window={
            "id": window.id,
            "external_source_id": window.external_source_id,
            "status": window.status,
            "message_count": window.message_count,
            "total_char_count": window.total_char_count,
            "first_message_at": window.first_message_at.isoformat() if window.first_message_at else None,
            "last_message_at": window.last_message_at.isoformat() if window.last_message_at else None,
        })

    if op == "list_messages":
        include = cmd.get("include_processed", True)
        async with AsyncSessionLocal() as db:
            async with db.begin():
                messages = await conversation_service.get_messages(
                    db, cmd["window_id"], include_processed=include
                )
        return _ok(messages=[
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "sequence_no": m.sequence_no,
                "processed": m.processed,
            }
            for m in messages
        ])

    if op == "flush":
        async with AsyncSessionLocal() as db:
            async with db.begin():
                await conversation_service.manual_flush(db, cmd["window_id"])
        return _ok()

    if op == "list_windows":
        async with AsyncSessionLocal() as db:
            async with db.begin():
                windows = await conversation_service.list_windows(
                    db, status=cmd.get("status"), limit=cmd.get("limit", 50)
                )
        return _ok(windows=[
            {
                "id": w.id,
                "external_source_id": w.external_source_id,
                "status": w.status,
                "message_count": w.message_count,
                "total_char_count": w.total_char_count,
            }
            for w in windows
        ])

    if op == "list_tasks":
        from sqlalchemy import select
        from core.models.conversation import AgentTask
        async with AsyncSessionLocal() as db:
            async with db.begin():
                stmt = select(AgentTask)
                if cmd.get("status"):
                    stmt = stmt.where(AgentTask.status == cmd["status"])
                if cmd.get("agent_type"):
                    stmt = stmt.where(AgentTask.agent_type == cmd["agent_type"])
                if cmd.get("batch_id"):
                    stmt = stmt.where(AgentTask.batch_id == cmd["batch_id"])
                stmt = stmt.order_by(AgentTask.created_at.desc()).limit(cmd.get("limit", 50))
                result = await db.scalars(stmt)
                tasks = list(result.all())
        return _ok(tasks=[
            {
                "id": t.id,
                "agent_type": t.agent_type,
                "instruction": t.instruction,
                "status": t.status,
                "batch_id": t.batch_id,
            }
            for t in tasks
        ])

    return _err(f"Unknown command: {op!r}")


# ── main loop ─────────────────────────────────────────────────────────────────

async def run_ipc() -> None:
    """Read JSON lines from stdin, write JSON lines to stdout."""
    from core.db.base import Base, engine, import_models
    from core.services.scheduler_service import scheduler

    # Initialise DB (same as FastAPI lifespan)
    import_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Start the background maintenance scheduler
    scheduler.start()

    loop = asyncio.get_event_loop()
    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin_reader), sys.stdin.buffer
    )

    stdout_writer_transport, stdout_writer_protocol = await loop.connect_write_pipe(
        asyncio.BaseProtocol, sys.stdout.buffer
    )

    def write_line(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    logger.info("IPC handler ready — reading JSON lines from stdin")

    while True:
        try:
            raw = await stdin_reader.readline()
        except Exception:
            break

        if not raw:
            break  # EOF

        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as exc:
            write_line(_err(f"JSON parse error: {exc}"))
            continue

        if cmd.get("cmd") == "shutdown":
            write_line(_ok())
            break

        try:
            result = await _handle(cmd)
        except Exception as exc:
            logger.exception("IPC handler error for cmd=%s", cmd.get("cmd"))
            result = _err(str(exc))

        write_line(result)

    await scheduler.stop()
    await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(run_ipc())


if __name__ == "__main__":
    main()
