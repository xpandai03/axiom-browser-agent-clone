"""
Proxy Sanity Check - Minimal, Standalone Test

This module tests ONLY whether the proxy is working.
It does NOT touch Uber Eats.
It does NOT retry.
It does NOT create multiple contexts.
It raises immediately on any failure.

Success criteria:
    🌐 PROXY SANITY IP: <non-datacenter IP>
"""

import asyncio
import logging
import os
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)


class ProxySanityError(Exception):
    """Raised when proxy sanity check fails."""
    pass


def build_proxy_config() -> Optional[dict]:
    """
    Build proxy config with embedded auth URL.

    Returns None if proxy is disabled or misconfigured.
    Raises ProxySanityError if enabled but malformed.
    """
    # Read env vars directly (no Pydantic, no caching)
    proxy_enabled = os.environ.get("API_PROXY_ENABLED", "false").lower() == "true"
    proxy_server = os.environ.get("API_PROXY_SERVER", "")
    proxy_username = os.environ.get("API_PROXY_USERNAME", "")
    proxy_password = os.environ.get("API_PROXY_PASSWORD", "")
    proxy_country = os.environ.get("API_PROXY_COUNTRY", "us")
    proxy_session = os.environ.get("API_PROXY_SESSION", "")

    logger.info("=" * 60)
    logger.info("PROXY SANITY CHECK - ENV VARS")
    logger.info("=" * 60)
    logger.info(f"  API_PROXY_ENABLED:  {proxy_enabled}")
    logger.info(f"  API_PROXY_SERVER:   {proxy_server or 'NOT SET'}")
    logger.info(f"  API_PROXY_USERNAME: {'SET' if proxy_username else 'NOT SET'}")
    logger.info(f"  API_PROXY_PASSWORD: {'SET' if proxy_password else 'NOT SET'}")
    logger.info(f"  API_PROXY_COUNTRY:  {proxy_country}")
    logger.info(f"  API_PROXY_SESSION:  {proxy_session or 'NOT SET'}")
    logger.info("=" * 60)

    if not proxy_enabled:
        logger.warning("⚠️ PROXY DISABLED - Will use direct connection")
        return None

    # Validate required fields
    if not proxy_server:
        raise ProxySanityError("API_PROXY_SERVER is required when proxy is enabled")
    if not proxy_username:
        raise ProxySanityError("API_PROXY_USERNAME is required when proxy is enabled")
    if not proxy_password:
        raise ProxySanityError("API_PROXY_PASSWORD is required when proxy is enabled")

    # Build IPRoyal-formatted username
    username_parts = [proxy_username]
    if proxy_country:
        username_parts.append(f"country-{proxy_country}")
    if proxy_session:
        username_parts.append(f"session-{proxy_session}")
    formatted_username = "_".join(username_parts)

    # Normalize server (strip protocol)
    server = proxy_server
    if server.startswith("http://"):
        server = server[7:]
    elif server.startswith("https://"):
        server = server[8:]

    # URL-encode credentials
    encoded_user = quote(formatted_username, safe='')
    encoded_pass = quote(proxy_password, safe='')

    # Build embedded-auth URL
    proxy_url = f"http://{encoded_user}:{encoded_pass}@{server}"

    # Extract host for logging (don't log credentials)
    host = server.split(":")[0] if ":" in server else server

    logger.info(f"✅ PROXY CONFIG BUILT")
    logger.info(f"   Host: {host}")
    logger.info(f"   Username format: {proxy_username[:4]}***_country-{proxy_country}_session-{proxy_session or 'none'}")
    logger.info(f"   Auth: EMBEDDED IN URL")

    return {"server": proxy_url}


async def run_proxy_sanity_check() -> dict:
    """
    Run a minimal proxy sanity check.

    Steps:
    1. Build proxy config
    2. Launch Chromium ONCE (no retries)
    3. Create ONE page (no fresh contexts)
    4. Navigate to ipify
    5. Extract IP
    6. Close browser
    7. Return result

    Returns:
        {
            "success": bool,
            "ip": str or None,
            "is_datacenter": bool or None,
            "error": str or None
        }
    """
    logger.info("")
    logger.info("=" * 60)
    logger.info("🔍 PROXY SANITY CHECK - START")
    logger.info("=" * 60)

    playwright_instance = None
    browser = None
    page = None

    try:
        # Step 1: Build proxy config
        logger.info("Step 1: Building proxy config...")
        proxy_config = build_proxy_config()

        # Step 2: Import and launch Playwright
        logger.info("Step 2: Importing Playwright...")
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ProxySanityError(f"Playwright not installed: {e}")

        logger.info("Step 3: Starting Playwright...")
        playwright_instance = await async_playwright().start()

        # Step 4: Launch browser with proxy
        logger.info("Step 4: Launching Chromium...")
        launch_kwargs = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        }

        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
            logger.info(f"   🔐 PROXY ATTACHED TO chromium.launch()")
        else:
            logger.warning(f"   ⚠️ NO PROXY - Direct connection")

        browser = await playwright_instance.chromium.launch(**launch_kwargs)
        logger.info("   ✅ Browser launched")

        # Step 5: Create ONE page
        logger.info("Step 5: Creating page...")
        page = await browser.new_page()
        logger.info("   ✅ Page created")

        # Step 6: Navigate to ipify
        logger.info("Step 6: Navigating to ipify...")
        response = await page.goto(
            "https://api.ipify.org?format=text",
            wait_until="domcontentloaded",
            timeout=30000
        )

        if not response:
            raise ProxySanityError("No response from ipify")

        if response.status != 200:
            raise ProxySanityError(f"ipify returned HTTP {response.status}")

        logger.info(f"   ✅ Navigation complete (HTTP {response.status})")

        # Step 7: Extract IP from body
        logger.info("Step 7: Extracting IP...")
        body_text = await page.text_content("body")

        if not body_text:
            raise ProxySanityError("Empty body from ipify")

        ip_address = body_text.strip()

        # Validate IP format
        import re
        if not re.match(r'^\d+\.\d+\.\d+\.\d+$', ip_address):
            raise ProxySanityError(f"Invalid IP format: {ip_address}")

        # Log the IP prominently
        logger.info("")
        logger.info("=" * 60)
        logger.info(f"🌐 PROXY SANITY IP: {ip_address}")
        logger.info("=" * 60)

        # Check if it looks like datacenter
        datacenter_prefixes = [
            "34.", "35.",      # GCP
            "104.",            # Various cloud
            "52.", "54.",      # AWS
            "18.", "3.", "13.", # AWS
            "23.", "44.",      # Various cloud
            "143.244.",        # Railway
            "66.241.",         # Railway
        ]

        is_datacenter = any(ip_address.startswith(prefix) for prefix in datacenter_prefixes)

        if is_datacenter:
            logger.warning(f"⚠️ IP {ip_address} LOOKS LIKE DATACENTER")
            logger.warning("   Proxy may not be working correctly!")
        else:
            logger.info(f"✅ IP {ip_address} does NOT match known datacenter ranges")

        logger.info("=" * 60)
        logger.info("🔍 PROXY SANITY CHECK - COMPLETE")
        logger.info("=" * 60)
        logger.info("")

        return {
            "success": True,
            "ip": ip_address,
            "is_datacenter": is_datacenter,
            "error": None
        }

    except ProxySanityError as e:
        logger.error(f"❌ PROXY SANITY FAILED: {e}")
        return {
            "success": False,
            "ip": None,
            "is_datacenter": None,
            "error": str(e)
        }

    except Exception as e:
        logger.exception(f"❌ UNEXPECTED ERROR: {e}")
        return {
            "success": False,
            "ip": None,
            "is_datacenter": None,
            "error": f"Unexpected: {e}"
        }

    finally:
        # Step 8: Close browser gracefully
        logger.info("Step 8: Cleaning up...")

        if page:
            try:
                await page.close()
                logger.info("   Page closed")
            except Exception as e:
                logger.warning(f"   Page close failed: {e}")

        if browser:
            try:
                await browser.close()
                logger.info("   Browser closed")
            except Exception as e:
                logger.warning(f"   Browser close failed: {e}")

        if playwright_instance:
            try:
                await playwright_instance.stop()
                logger.info("   Playwright stopped")
            except Exception as e:
                logger.warning(f"   Playwright stop failed: {e}")


# Synchronous wrapper for easy testing
def run_sanity_check_sync() -> dict:
    """Synchronous wrapper for run_proxy_sanity_check()."""
    return asyncio.run(run_proxy_sanity_check())


# =============================================================================
# FastAPI Router
# =============================================================================
from fastapi import APIRouter
from datetime import datetime

router = APIRouter(tags=["proxy"])


@router.get("/health/proxy-sanity")
async def proxy_sanity_endpoint():
    """
    Standalone proxy sanity check endpoint.

    Tests ONLY whether the proxy is working by:
    1. Launching browser ONCE with proxy
    2. Navigating to ipify
    3. Returning the outbound IP

    Does NOT touch Uber Eats.
    Does NOT retry.
    Does NOT create multiple contexts.

    Returns:
        {"success": true, "ip": "x.x.x.x", "is_datacenter": false}
    """
    result = await run_proxy_sanity_check()
    return {
        "timestamp": datetime.utcnow().isoformat(),
        **result
    }


if __name__ == "__main__":
    # Configure logging for standalone run
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    result = run_sanity_check_sync()
    print(f"\nResult: {result}")
