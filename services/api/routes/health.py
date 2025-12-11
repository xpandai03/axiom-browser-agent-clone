from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from datetime import datetime

from ..config import get_openai_api_key, get_config

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/fast", response_class=PlainTextResponse)
async def fast_health_check():
    """
    Ultra-lightweight health check for Railway/container orchestration.

    This endpoint:
    - Returns immediately with no dependencies
    - No database calls, no config loading, no heavy imports
    - Plain text response for minimal overhead
    """
    return "ok"


@router.get("")
async def health_check():
    """Health check endpoint with stealth/proxy status."""
    key, source = get_openai_api_key()
    config = get_config()

    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "axiom-api",
        "openai_key_loaded": key is not None,
        "openai_env_source": source,
        # Anti-bot bypass status
        "stealth_mode": config.stealth_mode,
        "proxy_enabled": config.proxy_enabled,
        "proxy_server": config.proxy_server[:30] + "..." if config.proxy_server and len(config.proxy_server) > 30 else config.proxy_server,
    }


@router.get("/ready")
async def readiness_check():
    """Readiness check for Kubernetes/Docker."""
    key, source = get_openai_api_key()
    return {
        "status": "ready",
        "timestamp": datetime.utcnow().isoformat(),
        "openai_key_loaded": key is not None,
        "openai_env_source": source,
    }


@router.get("/browser-check")
async def browser_check():
    """Test that Playwright browser launches successfully."""
    key, source = get_openai_api_key()
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
            "openai_key_loaded": key is not None,
            "openai_env_source": source,
        }
    except Exception as e:
        return {
            "status": "browser_failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
            "openai_key_loaded": key is not None,
            "openai_env_source": source,
        }
