"""Maintenance pipeline router.

POST /maintenance/trigger — manually trigger one maintenance pipeline run (fire-and-forget)
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
logger = logging.getLogger(__name__)


def _fire_maintenance(db: AsyncSession) -> None:
    """Schedule run_maintenance_pipeline as a background task."""
    from core.services.agent_service import run_maintenance_pipeline

    async def _run() -> None:
        try:
            await run_maintenance_pipeline(db)
        except Exception:
            logger.exception("Manual maintenance pipeline error")

    asyncio.ensure_future(_run())


@router.post("/trigger", status_code=202)
async def trigger_maintenance(db: AsyncSession = Depends(get_db)) -> dict:
    """Manually kick off one maintenance pipeline run (orchestrator → specialists)."""
    _fire_maintenance(db)
    return {"message": "Maintenance pipeline triggered (running in background)"}
