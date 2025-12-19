"""
Proxy Sanity Check - Minimal, Standalone Test

Tests SOCKS5 proxy with both HTTP and HTTPS endpoints.

WHY SOCKS5:
- HTTP proxies require CONNECT method for HTTPS tunneling
- Chromium's HTTP proxy auth causes ERR_PROXY_AUTH_UNSUPPORTED
- Even embedded auth fails: HTTPS traffic times out
- SOCKS5 operates at TCP level, tunnels ALL traffic
- Chromium DOES support username/password auth for SOCKS5

Test endpoints:
- HTTP:  http://httpbin.org/ip
- HTTPS: https://api.ipify.org?format=text

Success criteria:
    🌐 PROXY SANITY IP: <non-datacenter IP>
"""

import asyncio
import logging
import os
import re
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ProxySanityError(Exception):
    """Raised when proxy sanity check fails."""
    pass


def build_proxy_config() -> Optional[dict]:
    """
    Build SOCKS5 proxy config with username/password auth.

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

    # Normalize server (strip any protocol prefix)
    server = proxy_server
    for prefix in ["socks5://", "socks4://", "http://", "https://"]:
        if server.startswith(prefix):
            server = server[len(prefix):]
            break

    # Extract host for logging
    host = server.split(":")[0] if ":" in server else server

    logger.info(f"✅ SOCKS5 PROXY CONFIG BUILT")
    logger.info(f"   Protocol: SOCKS5")
    logger.info(f"   Server:   socks5://{server}")
    logger.info(f"   Host:     {host}")
    logger.info(f"   Username: {proxy_username[:4]}***_country-{proxy_country}_session-{proxy_session or 'none'}")
    logger.info(f"   Auth:     username/password (separate fields)")

    # SOCKS5 with separate username/password (Chromium supports this!)
    return {
        "server": f"socks5://{server}",
        "username": formatted_username,
        "password": proxy_password,
    }


async def test_single_url(page, url: str, protocol_name: str) -> dict:
    """Test a single URL and return result."""
    logger.info(f"  Testing {protocol_name}: {url}")

    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if not response:
            return {"success": False, "error": "No response", "ip": None}

        if response.status != 200:
            return {"success": False, "error": f"HTTP {response.status}", "ip": None}

        body_text = await page.text_content("body")
        if not body_text:
            return {"success": False, "error": "Empty body", "ip": None}

        # Extract IP from response
        # httpbin.org returns JSON: {"origin": "1.2.3.4"}
        # ipify returns plain text: 1.2.3.4
        ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', body_text)
        if ip_match:
            ip = ip_match.group(1)
            logger.info(f"  ✅ {protocol_name} SUCCESS: {ip}")
            return {"success": True, "ip": ip, "error": None}
        else:
            return {"success": False, "error": f"No IP found in: {body_text[:100]}", "ip": None}

    except Exception as e:
        error_msg = str(e)
        # Truncate long error messages
        if len(error_msg) > 100:
            error_msg = error_msg[:100] + "..."
        logger.error(f"  ❌ {protocol_name} FAILED: {error_msg}")
        return {"success": False, "error": error_msg, "ip": None}


async def run_proxy_sanity_check() -> dict:
    """
    Run a minimal proxy sanity check with dual-protocol testing.

    Tests:
    1. HTTP endpoint:  http://httpbin.org/ip
    2. HTTPS endpoint: https://api.ipify.org?format=text

    Returns:
        {
            "success": bool,
            "ip": str or None,
            "is_datacenter": bool or None,
            "http_test": {...},
            "https_test": {...},
            "protocol": "SOCKS5",
            "error": str or None
        }
    """
    logger.info("")
    logger.info("=" * 60)
    logger.info("🔍 PROXY SANITY CHECK - START (SOCKS5)")
    logger.info("=" * 60)

    playwright_instance = None
    browser = None
    page = None

    result = {
        "success": False,
        "ip": None,
        "is_datacenter": None,
        "protocol": "SOCKS5",
        "http_test": None,
        "https_test": None,
        "error": None,
    }

    try:
        # Step 1: Build proxy config
        logger.info("Step 1: Building SOCKS5 proxy config...")
        proxy_config = build_proxy_config()

        # Step 2: Import Playwright
        logger.info("Step 2: Importing Playwright...")
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ProxySanityError(f"Playwright not installed: {e}")

        # Step 3: Start Playwright
        logger.info("Step 3: Starting Playwright...")
        playwright_instance = await async_playwright().start()

        # Step 4: Launch browser with SOCKS5 proxy
        logger.info("Step 4: Launching Chromium with SOCKS5 proxy...")
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
            logger.info(f"   🔐 SOCKS5 PROXY ATTACHED: {proxy_config['server']}")
            logger.info(f"   👤 Username: {proxy_config['username'][:20]}...")
        else:
            logger.warning(f"   ⚠️ NO PROXY - Direct connection")

        browser = await playwright_instance.chromium.launch(**launch_kwargs)
        logger.info("   ✅ Browser launched")

        # Step 5: Create page
        logger.info("Step 5: Creating page...")
        page = await browser.new_page()
        logger.info("   ✅ Page created")

        # Step 6: Test HTTP endpoint
        logger.info("Step 6: Testing HTTP endpoint...")
        http_result = await test_single_url(
            page,
            "http://httpbin.org/ip",
            "HTTP"
        )
        result["http_test"] = http_result

        # Step 7: Test HTTPS endpoint
        logger.info("Step 7: Testing HTTPS endpoint...")
        https_result = await test_single_url(
            page,
            "https://api.ipify.org?format=text",
            "HTTPS"
        )
        result["https_test"] = https_result

        # Step 8: Analyze results
        logger.info("Step 8: Analyzing results...")

        # Prefer HTTPS result, fallback to HTTP
        if https_result["success"]:
            result["ip"] = https_result["ip"]
            result["success"] = True
        elif http_result["success"]:
            result["ip"] = http_result["ip"]
            result["success"] = True
            logger.warning("⚠️ HTTPS failed but HTTP worked - partial proxy support")
        else:
            result["error"] = f"Both protocols failed. HTTP: {http_result['error']}, HTTPS: {https_result['error']}"
            raise ProxySanityError(result["error"])

        # Log the IP prominently
        ip_address = result["ip"]
        logger.info("")
        logger.info("=" * 60)
        logger.info(f"🌐 PROXY SANITY IP: {ip_address}")
        logger.info("=" * 60)

        # Check if datacenter
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
        result["is_datacenter"] = is_datacenter

        if is_datacenter:
            logger.warning(f"⚠️ IP {ip_address} LOOKS LIKE DATACENTER")
            logger.warning("   Proxy may not be routing correctly!")
        else:
            logger.info(f"✅ IP {ip_address} does NOT match known datacenter ranges")

        # Summary
        logger.info("")
        logger.info("=" * 60)
        logger.info("PROTOCOL TEST SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  HTTP  (httpbin.org):    {'✅ PASS' if http_result['success'] else '❌ FAIL'} - {http_result.get('ip') or http_result.get('error')}")
        logger.info(f"  HTTPS (api.ipify.org):  {'✅ PASS' if https_result['success'] else '❌ FAIL'} - {https_result.get('ip') or https_result.get('error')}")
        logger.info(f"  Final IP:               {ip_address}")
        logger.info(f"  Is Datacenter:          {is_datacenter}")
        logger.info("=" * 60)
        logger.info("🔍 PROXY SANITY CHECK - COMPLETE")
        logger.info("=" * 60)
        logger.info("")

        return result

    except ProxySanityError as e:
        logger.error(f"❌ PROXY SANITY FAILED: {e}")
        result["error"] = str(e)
        return result

    except Exception as e:
        logger.exception(f"❌ UNEXPECTED ERROR: {e}")
        result["error"] = f"Unexpected: {e}"
        return result

    finally:
        # Cleanup
        logger.info("Cleanup: Closing browser...")

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


# Synchronous wrapper for testing
def run_sanity_check_sync() -> dict:
    """Synchronous wrapper for run_proxy_sanity_check()."""
    return asyncio.run(run_proxy_sanity_check())


# =============================================================================
# FastAPI Router
# =============================================================================
from fastapi import APIRouter

router = APIRouter(tags=["proxy"])


@router.get("/health/proxy-sanity")
async def proxy_sanity_endpoint():
    """
    Standalone SOCKS5 proxy sanity check endpoint.

    Tests proxy with BOTH protocols:
    - HTTP:  http://httpbin.org/ip
    - HTTPS: https://api.ipify.org?format=text

    Returns:
        {
            "success": true,
            "ip": "x.x.x.x",
            "is_datacenter": false,
            "protocol": "SOCKS5",
            "http_test": {"success": true, "ip": "x.x.x.x"},
            "https_test": {"success": true, "ip": "x.x.x.x"}
        }
    """
    result = await run_proxy_sanity_check()
    return {
        "timestamp": datetime.utcnow().isoformat(),
        **result
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    result = run_sanity_check_sync()
    print(f"\nResult: {result}")
