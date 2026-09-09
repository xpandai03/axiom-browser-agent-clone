"""
Route for attaching a survey PDF to an EXISTING TherapyNotes patient chart.

Parallel surface to /api/tn/create-patient-with-schedule, deliberately sharing
nothing with it but the mechanics. Gated by the same X-API-Key middleware
(path starts with /api/tn).

Endpoint:
- POST /api/tn/attach-survey-to-chart
"""

import logging

from fastapi import APIRouter, HTTPException

from shared.schemas.survey_attach import SurveyAttachInput, SurveyAttachOutput

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tn", tags=["survey-attach"])


@router.post("/attach-survey-to-chart", response_model=SurveyAttachOutput)
async def attach_survey_to_chart(request: SurveyAttachInput):
    """
    Find a patient, VERIFY it is the right person, then attach a PDF.

    Verification is the point: name, date of birth, mobile phone and clinician
    must all match before anything is uploaded. Any mismatch, any field that
    cannot be read, or any ambiguity in the search results refuses and names the
    reason — a survey filed to the wrong chart is a PHI disclosure that cannot be
    undone, whereas a refusal costs a staff member a manual upload.
    """
    try:
        # Correlation ids only — no patient values in this line.
        logger.info(
            f"[ATTACH] request run_id={request.run_id} contact_id={request.contact_id} "
            f"document='{request.document_name}'"
        )

        # Lazy import so Playwright is not loaded at startup.
        from ..mcp_runtime import PlaywrightRuntime
        from ..survey_attach_executor import run_survey_attach

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)
        try:
            result = await run_survey_attach(runtime, request)
        finally:
            await runtime.close()

        if result.status == "success":
            logger.info(
                f"[ATTACH] attached '{result.document_name}' | url={result.tn_patient_url}"
            )
        else:
            logger.warning(
                f"[ATTACH] refused at {result.failed_phase}: "
                f"{result.failure_reason} — {result.message}"
            )
        return result

    except Exception as e:
        logger.exception(f"[ATTACH] endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
