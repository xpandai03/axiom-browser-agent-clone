import os
from fastapi import APIRouter
from datetime import datetime

router = APIRouter(prefix="/health", tags=["health"])


def _check_openai_key() -> bool:
    """Check if OpenAI API key is available."""
    key = (
        os.environ.get("API_OPENAI_API_KEY") or
        os.environ.get("OPENAI_API_KEY") or
        os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
    )
    return key is not None and len(key) > 0


@router.get("")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "axiom-api",
        "openai_key_loaded": _check_openai_key(),
    }


@router.get("/ready")
async def readiness_check():
    """Readiness check for Kubernetes/Docker."""
    return {
        "status": "ready",
        "timestamp": datetime.utcnow().isoformat(),
        "openai_key_loaded": _check_openai_key(),
    }


@router.get("/browser-check")
async def browser_check():
    """Test that Playwright browser launches successfully."""
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
            "openai_key_loaded": _check_openai_key(),
        }
    except Exception as e:
        return {
            "status": "browser_failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
            "openai_key_loaded": _check_openai_key(),
        }
