"""
Selector smoke routes. Gated by the same X-API-Key middleware as every /api/tn path.

- GET /api/tn/selector-smoke       run it now: ONE TherapyNotes login, read-only
- GET /api/tn/selector-smoke/last  the last result in memory; no TherapyNotes action

The daily run (03:00 America/Denver) is started from the app lifespan.
"""

import logging

from fastapi import APIRouter, HTTPException

from ..selector_smoke import SelectorSmokeResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tn", tags=["selector-smoke"])


@router.get("/selector-smoke", response_model=SelectorSmokeResult)
async def selector_smoke():
    from .. import selector_smoke as smoke
    logger.info("[SMOKE] run requested")
    return await smoke.run_selector_smoke()


@router.get("/selector-smoke/last", response_model=SelectorSmokeResult)
async def selector_smoke_last():
    from .. import selector_smoke as smoke
    if smoke.LAST_RESULT is None:
        raise HTTPException(status_code=404, detail="No selector smoke has run since the last deploy")
    return smoke.LAST_RESULT
