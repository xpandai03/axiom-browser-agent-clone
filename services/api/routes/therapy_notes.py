"""
API routes for the TherapyNotes patient creation executor.

Endpoints:
- POST /api/tn/create-patient - Create a patient in TherapyNotes (requires X-API-Key)
- POST /api/tn/test-login     - Debug: incremental phase test
- GET  /api/tn/test           - Health check for TN executor
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from shared.schemas.therapy_notes import TNPatientInput, TNExecutorOutput

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tn", tags=["therapy-notes"])


@router.post("/create-patient", response_model=TNExecutorOutput)
async def create_patient(request: TNPatientInput):
    """
    Create a patient in TherapyNotes via headless browser automation.

    Requires X-API-Key header (enforced by middleware in app.py).

    Executes a deterministic 6-phase workflow:
      0. ENTRY       — Navigate to TN, fill practice code
      1. LOGIN       — Authenticate with service account
      2. NAVIGATE    — Sidebar -> Patients page
      3. DETECT_FORM — Click + New Patient, wait for form
      4. FILL        — Fill 8 required fields
      5. SAVE        — Submit and confirm creation

    Returns structured output with per-phase logs, tn_patient_url, and tn_patient_id.
    """
    try:
        logger.info(f"TN patient creation: {request.first_name} {request.last_name}")

        # Lazy import to avoid loading Playwright at startup
        from ..mcp_runtime import PlaywrightRuntime
        from ..tn_executor import run_tn_patient_creation

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)

        try:
            result = await run_tn_patient_creation(runtime, request)
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
                f"TN patient created: {result.patient_name} | "
                f"url={result.tn_patient_url} id={result.tn_patient_id}"
            )
        else:
            logger.warning(
                f"TN patient creation failed at {result.failed_phase}: "
                f"{result.failure_reason} — {result.error_message}"
            )

        return result

    except Exception as e:
        logger.exception(f"TN create-patient endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test-login")
async def test_login(patient: Optional[TNPatientInput] = None):
    """
    Debug endpoint: incremental phase test.

    Runs Phases 0-3 always (entry, login, navigate, detect form).
    If a TNPatientInput body is provided, also runs Phase 4 (fill) and Phase 5 (save).
    """
    try:
        logger.info("TN login test starting")

        from ..mcp_runtime import PlaywrightRuntime
        from ..tn_executor import TNExecutor
        from ..config import get_tn_credentials
        from shared.schemas.therapy_notes import TNPhase, TNExecutorOutput as Output

        import time

        # Fail fast on missing credentials
        try:
            credentials = get_tn_credentials()
        except Exception as e:
            logger.error(f"TN credential validation failed: {e}")
            return Output.failure(
                phase=TNPhase.ENTRY,
                reason="login_failed",
                message=(
                    "Missing TherapyNotes credentials. Required env vars: "
                    "THERAPYNOTES_PRACTICE_CODE, THERAPYNOTES_USERNAME, "
                    "THERAPYNOTES_PASSWORD"
                ),
                logs=[],
                duration_ms=0,
            )

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)
        executor = TNExecutor(runtime, credentials)
        executor._start_time = time.time()

        try:
            executor._page = await runtime.ensure_browser()

            # Phase 0: Entry
            entry_ok = await executor._phase_entry()
            if not entry_ok:
                return executor._build_failure_output()

            # Phase 1: Login
            login_ok = await executor._phase_login()
            if not login_ok:
                return executor._build_failure_output()

            # Phase 2: Navigate to Patients page
            navigate_ok = await executor._phase_navigate()
            if not navigate_ok:
                return executor._build_failure_output()

            # Phase 3: Detect New Patient form fields
            detect_ok = await executor._phase_detect_form()
            if not detect_ok:
                return executor._build_failure_output()

            # Phase 4: Fill required fields (only if patient data provided)
            if patient:
                fill_ok = await executor._phase_fill_required(patient)
                if not fill_ok:
                    return executor._build_failure_output()

                # Phase 5: Save Patient
                save_ok = await executor._phase_save_patient(patient)
                if not save_ok:
                    return executor._build_failure_output()

                return Output.success(
                    patient_name=f"{patient.first_name} {patient.last_name}",
                    logs=executor._logs,
                    duration_ms=executor._elapsed_ms(),
                    tn_patient_url=getattr(executor, "_tn_patient_url", None),
                    tn_patient_id=getattr(executor, "_tn_patient_id", None),
                )

            # Phases 0-3 only (no patient data)
            return Output.success(
                patient_name="FORM_DETECT_TEST",
                logs=executor._logs,
                duration_ms=executor._elapsed_ms(),
            )

        finally:
            await runtime.close()

    except Exception as e:
        logger.exception(f"TN login test failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/test")
async def test_endpoint():
    """Health check for the TN executor route."""
    return {
        "status": "ok",
        "message": "TherapyNotes executor endpoint is active",
        "endpoints": [
            "POST /api/tn/create-patient (requires X-API-Key header)",
            "POST /api/tn/test-login (debug)",
        ],
    }
