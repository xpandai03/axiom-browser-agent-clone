"""
Route for reading ACTIVE PATIENT IDENTITY from TherapyNotes.

Gated by the same X-API-Key middleware as every other /api/tn path (the gate is
path-prefix based, so GET is authenticated exactly like POST).

GET, not POST, and deliberately: this endpoint takes no input and changes
nothing in TherapyNotes. The method is part of saying so.

Endpoint:
- GET /api/tn/active-patients
"""

import logging

from fastapi import APIRouter, HTTPException

from shared.schemas.active_patients import ActivePatientsOutput

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tn", tags=["active-patients"])


@router.get("/active-patients", response_model=ActivePatientsOutput)
async def active_patients():
    """
    One login, then every clinician's Active patient list, read row by row.

    Returns raw values only — no normalisation, no de-duplication, no matching
    to CRM contacts. A patient assigned to several clinicians appears under each,
    because that is what the page shows; collapsing on chart id is the CRM's
    decision and it needs the occurrences to make it.

    A clinician whose rows cannot be read is returned as a failure rather than as
    an empty list: an empty list and an unreadable page look identical to a
    caller, and one of them means half a caseload has silently vanished.
    """
    try:
        logger.info("[PATIENTS] active patient identity pass requested")

        # Lazy import so Playwright is not loaded at startup.
        from ..mcp_runtime import PlaywrightRuntime
        from ..active_patients_executor import run_active_patients

        runtime = PlaywrightRuntime(
            skip_proxy=True, skip_resource_blocking=True, skip_stealth=True
        )
        try:
            result = await run_active_patients(runtime)
        finally:
            await runtime.close()

        logger.info(
            f"[PATIENTS] pass {result.status}: {result.succeeded} ok, "
            f"{result.failed} failed, {result.total_rows} rows, "
            f"{result.distinct_chart_ids} distinct, {result.duration_ms}ms"
        )
        return result

    except Exception as e:
        logger.exception(f"[PATIENTS] endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
