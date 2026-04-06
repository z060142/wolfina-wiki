"""Ingest pipeline router.

POST /ingest        — trigger a full ingest pipeline run (async, fire-and-forget)
POST /ingest/force  — re-ingest specific files regardless of content hash change
GET  /ingest/status — list FileIngestRecord entries
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from core.models.ingest import FileIngestRecord
from core.settings import settings

router = APIRouter(prefix="/ingest", tags=["ingest"])
logger = logging.getLogger(__name__)


class ForceIngestRequest(BaseModel):
    paths: list[str]


def _fire_ingest(force_paths: list[str] | None = None) -> None:
    """Schedule run_ingest_pipeline as a background task."""
    from core.db.base import AsyncSessionLocal
    from core.services.agent_service import run_ingest_pipeline

    async def _run() -> None:
        async with AsyncSessionLocal() as db:
            try:
                await run_ingest_pipeline(db, force_paths=force_paths)
            except Exception:
                logger.exception("Background ingest pipeline error")

    asyncio.ensure_future(_run())


@router.post("", status_code=202)
async def trigger_ingest() -> dict:
    """Trigger the ingest pipeline for all new/changed files."""
    _fire_ingest()
    return {"status": "accepted", "message": "Ingest pipeline queued."}


@router.post("/force", status_code=202)
async def force_ingest(body: ForceIngestRequest) -> dict:
    """Re-ingest specific files regardless of hash change."""
    if not body.paths:
        return {"status": "accepted", "message": "No paths provided — nothing to do."}
    _fire_ingest(force_paths=body.paths)
    return {
        "status": "accepted",
        "message": f"Force ingest queued for {len(body.paths)} file(s).",
        "paths": body.paths,
    }


@router.get("/status")
async def ingest_status(
    status: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List FileIngestRecord entries."""
    from sqlalchemy import select

    stmt = select(FileIngestRecord).order_by(FileIngestRecord.last_scanned_at.desc())
    if status:
        stmt = stmt.where(FileIngestRecord.status == status)
    stmt = stmt.limit(min(limit, 200))

    result = await db.scalars(stmt)
    records = list(result.all())
    return {
        "count": len(records),
        "records": [
            {
                "id": r.id,
                "path": r.path,
                "status": r.status,
                "summary": r.summary,
                "related_page_ids": r.related_page_ids,
                "last_scanned_at": r.last_scanned_at.isoformat() if r.last_scanned_at else None,
                "last_processed_at": r.last_processed_at.isoformat() if r.last_processed_at else None,
                "error_message": r.error_message,
            }
            for r in records
        ],
    }
