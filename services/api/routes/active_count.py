"""
Route for reading per-clinician ACTIVE CLIENT COUNTS from TherapyNotes.

Gated by the same X-API-Key middleware as every other /api/tn path (the gate is
path-prefix based, so GET is authenticated exactly like POST).

GET, not POST, and deliberately: this endpoint takes no input and changes
nothing in TherapyNotes. The method is part of saying so.

Endpoint:
- GET /api/tn/active-client-counts
"""

import logging

from fastapi import APIRouter, HTTPException

from shared.schemas.active_count import ActiveCountOutput

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tn", tags=["active-count"])


@router.get("/active-client-counts", response_model=ActiveCountOutput)
async def active_client_counts():
    """
    One login, then every clinician's active patient count, read off the page.

    Returns raw values only — no subtraction, no normalisation, no matching to
    CRM providers. Test Anna records are reported per clinician and NEVER
    subtracted; whether they should be excluded is the client's call.

    A clinician whose count cannot be parsed is returned as a failure rather
    than a number: a wrong denominator silently inflates a percentage, and one
    bad read must not discard the ones that worked.
    """
    try:
        logger.info("[COUNT] active client count pass requested")

        # Lazy import so Playwright is not loaded at startup.
        from ..mcp_runtime import PlaywrightRuntime
        from ..active_count_executor import run_active_count

        runtime = PlaywrightRuntime(
            skip_proxy=True, skip_resource_blocking=True, skip_stealth=True
        )
        try:
            result = await run_active_count(runtime)
        finally:
            await runtime.close()

        logger.info(
            f"[COUNT] pass {result.status}: {result.succeeded} ok, "
            f"{result.failed} failed, {result.duration_ms}ms"
        )
        return result

    except Exception as e:
        logger.exception(f"[COUNT] endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
