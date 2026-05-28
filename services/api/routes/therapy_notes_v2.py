"""
API routes for the TherapyNotes patient creation executor (V2 — beta).

Step 1 clone of the create-patient handler. Behavior-identical to
routes/therapy_notes.py:create_patient, exposed at a parallel path so the TFC CRM
can drive a beta button against it without touching the production route. This V2
surface is where Step 2 adds PDF upload + appointment scheduling phases.

Endpoints:
- POST /api/tn/create-patient-with-schedule - Create a patient in TherapyNotes (requires X-API-Key)

Auth: gated by the same X-API-Key middleware in app.py (path starts with /api/tn).
No debug endpoints are cloned (no V2 test-login / test).
"""

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from shared.schemas.therapy_notes_v2 import TNPatientInputV2, TNExecutorOutputV2

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tn", tags=["therapy-notes-v2"])


@router.post("/create-patient-with-schedule", response_model=TNExecutorOutputV2)
async def create_patient_with_schedule(request: TNPatientInputV2):
    """
    Create a patient in TherapyNotes via headless browser automation (V2 beta).

    Requires X-API-Key header (enforced by middleware in app.py).

    Step 1: executes the same deterministic 6-phase workflow as the production
    create-patient route. Step 2 will extend this with PDF upload + scheduling.

      0. ENTRY       — Navigate to TN, fill practice code
      1. LOGIN       — Authenticate with service account
      2. NAVIGATE    — Sidebar -> Patients page
      3. DETECT_FORM — Click + New Patient, wait for form
      4. FILL        — Fill 8 required fields
      5. SAVE        — Submit and confirm creation

    Returns structured output with per-phase logs, tn_patient_url, and tn_patient_id.
    """
    try:
        logger.info(f"TN V2 patient creation: {request.first_name} {request.last_name}")

        # Lazy import to avoid loading Playwright at startup
        from ..mcp_runtime import PlaywrightRuntime
        from ..tn_executor_v2 import run_tn_v2_patient_creation

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)

        try:
            result = await run_tn_v2_patient_creation(runtime, request)
        finally:
            await runtime.close()

        # Concurrency guard: return 429 if another execution is in progress
        if (
            result.status == "error"
            and result.error_message == "Another patient creation is already in progress"
        ):
            return JSONResponse(
                status_code=429,
                content=result.model_dump(mode="json"),
            )

        if result.status == "success":
            logger.info(
                f"TN V2 patient created: {result.patient_name} | "
                f"url={result.tn_patient_url} id={result.tn_patient_id}"
            )
        else:
            logger.warning(
                f"TN V2 patient creation failed at {result.failed_phase}: "
                f"{result.failure_reason} — {result.error_message}"
            )

        return result

    except Exception as e:
        logger.exception(f"TN V2 create-patient-with-schedule endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
