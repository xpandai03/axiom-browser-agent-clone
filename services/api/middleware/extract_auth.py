"""
API key middleware for /api/extract/* routes.

Mirrors the TN middleware pattern in services/api/app.py. Reads
`EXTRACT_API_KEY` from environment and rejects any request to
/api/extract/* whose `X-API-Key` header doesn't match.

Required env var: EXTRACT_API_KEY
"""
import logging
import os

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def extract_auth_middleware(request, call_next):
    path = request.url.path
    if not path.startswith("/api/extract"):
        return await call_next(request)

    api_key = os.environ.get("EXTRACT_API_KEY")
    if not api_key:
        logger.error("EXTRACT_API_KEY not configured on server — rejecting /api/extract request")
        return JSONResponse(
            status_code=500,
            content={"error": "EXTRACT_API_KEY not configured on server"},
        )

    provided = request.headers.get("X-API-Key")
    if provided != api_key:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing API key"},
        )

    return await call_next(request)
