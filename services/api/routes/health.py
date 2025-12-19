"""
Health check endpoints for Railway/container orchestration.

CRITICAL: /health/fast MUST be completely independent with ZERO imports
from our codebase to guarantee it responds instantly during startup.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


# =============================================================================
# CRITICAL: This endpoint MUST work even if the rest of the app is broken
# - NO imports from our codebase
# - NO config loading
# - NO database calls
# - NO Playwright imports
# - ALWAYS returns HTTP 200
# =============================================================================
@router.get("/health/fast")
async def fast_health():
    """Minimal healthcheck for Railway - always returns 200."""
    return JSONResponse(content={"ok": True}, status_code=200)


# =============================================================================
# Secondary endpoints - these can have dependencies
# =============================================================================
@router.get("/health")
async def health_check():
    """Full health check with service status."""
    # Lazy import to avoid blocking fast healthcheck
    from datetime import datetime
    try:
        from ..config import get_openai_api_key, get_config
        key, source = get_openai_api_key()
        config = get_config()
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "service": "axiom-api",
            "openai_key_loaded": key is not None,
            "openai_env_source": source,
            "stealth_mode": config.stealth_mode,
            "proxy_enabled": config.proxy_enabled,
            "proxy_server": config.proxy_server[:30] + "..." if config.proxy_server else None,
            "proxy_configured": config.proxy_config is not None,
        }
    except Exception as e:
        return {
            "status": "degraded",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
        }


@router.get("/health/ready")
async def readiness_check():
    """Readiness check for Kubernetes/Docker."""
    from datetime import datetime
    try:
        from ..config import get_openai_api_key
        key, source = get_openai_api_key()
        return {
            "status": "ready",
            "timestamp": datetime.utcnow().isoformat(),
            "openai_key_loaded": key is not None,
            "openai_env_source": source,
        }
    except Exception as e:
        return {
            "status": "not_ready",
            "error": str(e),
        }


@router.get("/health/browser-check")
async def browser_check():
    """Test that Playwright browser launches successfully."""
    from datetime import datetime
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()
            await page.goto("https://example.com")
            title = await page.title()
            await browser.close()

        return {
            "status": "browser_working",
            "page_title": title,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {
            "status": "browser_failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
        }


@router.get("/health/proxy-sanity")
async def proxy_sanity():
    """
    Standalone proxy sanity check.

    Tests ONLY whether the proxy is working by:
    1. Launching browser ONCE with proxy
    2. Navigating to ipify
    3. Returning the outbound IP

    Does NOT touch Uber Eats.
    Does NOT retry.
    Does NOT create multiple contexts.

    Success: {"success": true, "ip": "x.x.x.x", "is_datacenter": false}
    """
    from datetime import datetime
    try:
        from ..proxy_sanity import run_proxy_sanity_check

        result = await run_proxy_sanity_check()

        return {
            "timestamp": datetime.utcnow().isoformat(),
            **result
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
        }
