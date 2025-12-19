"""
MCP Runtime for Playwright browser automation.

CRITICAL FOR RAILWAY DEPLOYMENT:
- Playwright is LAZY imported only when first needed
- No heavy imports at module load time
- This ensures FastAPI starts quickly and healthchecks pass

The runtime can operate in two modes:
1. Native MCP mode: Tools called through Cursor's MCP protocol
2. Direct Playwright mode: Fallback for standalone execution
"""
from __future__ import annotations  # PEP 563: Postponed evaluation of annotations

import asyncio
import base64
import logging
import os
import random
from typing import Any, Dict, Optional, List, TYPE_CHECKING

logger = logging.getLogger(__name__)

# =============================================================================
# CRITICAL: All heavy imports are LAZY - not loaded at module import time
# =============================================================================

# Playwright - lazy loaded on first browser operation
PLAYWRIGHT_AVAILABLE = None  # None = not yet checked
_playwright_module = None

def _get_playwright():
    """Lazy import Playwright to avoid blocking app startup."""
    global PLAYWRIGHT_AVAILABLE, _playwright_module
    if PLAYWRIGHT_AVAILABLE is None:
        try:
            from playwright.async_api import async_playwright, Browser, BrowserContext, Page
            _playwright_module = {
                'async_playwright': async_playwright,
                'Browser': Browser,
                'BrowserContext': BrowserContext,
                'Page': Page,
            }
            PLAYWRIGHT_AVAILABLE = True
            logger.info("Playwright loaded successfully (lazy)")
        except ImportError:
            PLAYWRIGHT_AVAILABLE = False
            _playwright_module = None
            logger.warning("Playwright not available - browser operations will fail")
    return _playwright_module

# Stealth mode - lazy import to avoid blocking startup
STEALTH_AVAILABLE = None  # None = not yet checked
_stealth_async = None

def _get_stealth_async():
    """Lazy import playwright-stealth to avoid blocking app startup."""
    global STEALTH_AVAILABLE, _stealth_async
    if STEALTH_AVAILABLE is None:
        try:
            from playwright_stealth import stealth_async
            _stealth_async = stealth_async
            STEALTH_AVAILABLE = True
            logger.info("playwright-stealth loaded successfully")
        except ImportError:
            STEALTH_AVAILABLE = False
            logger.warning("playwright-stealth not available - stealth mode disabled")
    return _stealth_async if STEALTH_AVAILABLE else None

# Config - lazy loaded
_cached_config = None

def _get_config():
    """Lazy import config to avoid pydantic validation at import time."""
    global _cached_config
    if _cached_config is None:
        from .config import get_config
        _cached_config = get_config()
    return _cached_config


class PlaywrightRuntime:
    """
    Playwright runtime for browser automation.

    This class manages a Playwright browser instance and provides
    methods that map to MCP tool calls.

    CRITICAL: No Playwright imports happen until ensure_browser() is called.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None  # Type: Optional[Browser] - lazy typed
        self._context = None  # Type: Optional[BrowserContext] - lazy typed
        self._page = None     # Type: Optional[Page] - lazy typed
        self._headless = os.environ.get("BROWSER_HEADLESS", "true").lower() == "true"
        self._config = None   # Lazy loaded on first use

    async def ensure_browser(self) -> Page:
        """Ensure browser is running and return the page."""
        if self._page is None:
            await self._start_browser()
        return self._page

    async def _human_delay(self, min_ms: int = 100, max_ms: int = 500) -> None:
        """Add random human-like delay between actions."""
        delay = random.randint(min_ms, max_ms) / 1000
        await asyncio.sleep(delay)

    async def _start_browser(self, skip_stealth: bool = False) -> None:
        """
        Start the Playwright browser with proxy support.

        Args:
            skip_stealth: If True, skip playwright-stealth patches (helps with some sites)
        """
        # Lazy load Playwright
        pw = _get_playwright()
        if not pw:
            raise RuntimeError("Playwright is not installed")

        # Lazy load config
        if self._config is None:
            self._config = _get_config()

        config = self._config
        proxy_config = config.proxy_config

        # ========================================================================
        # EXPLICIT PROXY LOGGING (credentials embedded in URL for Chromium)
        # ========================================================================
        logger.info("=" * 70)
        logger.info("PROXY CONFIGURATION STATUS")
        logger.info("=" * 70)
        logger.info(f"  API_PROXY_ENABLED:  {config.proxy_enabled}")
        logger.info(f"  API_PROXY_SERVER:   {config.proxy_server}")
        logger.info(f"  API_PROXY_USERNAME: {'SET' if config.proxy_username else 'NOT SET'}")
        logger.info(f"  API_PROXY_PASSWORD: {'SET' if config.proxy_password else 'NOT SET'}")
        logger.info(f"  API_PROXY_COUNTRY:  {config.proxy_country}")
        logger.info(f"  API_PROXY_SESSION:  {config.proxy_session}")

        if proxy_config:
            # Log the host only (credentials are embedded in URL, don't log them!)
            logger.info(f"  ✅ PROXY CONFIG BUILT (auth embedded in URL)")
            logger.info(f"  🔐 PROXY ENABLED: {config.proxy_server_host} (auth embedded)")
            logger.info(f"  📤 Config: {config.proxy_config_display}")
        else:
            logger.error("  ❌ PROXY CONFIG IS NONE - WILL USE DATACENTER IP!")
            logger.error("  ❌ This will cause Uber Eats to return degraded page!")
            logger.error("  Required env vars:")
            logger.error("    API_PROXY_ENABLED=true")
            logger.error("    API_PROXY_SERVER=geo.iproyal.com:12321")
            logger.error("    API_PROXY_USERNAME=<your_username>")
            logger.error("    API_PROXY_PASSWORD=<your_password>")
        logger.info("=" * 70)

        async_playwright = pw['async_playwright']
        self._playwright = await async_playwright().start()

        # Browser launch args
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-sync",
            "--no-first-run",
            "--single-process",
            "--disable-blink-features=AutomationControlled",
        ]

        # ========================================================================
        # STEP 2: PROXY APPLIED AT BROWSER LAUNCH (NOT CONTEXT)
        # ========================================================================
        launch_kwargs = {
            "headless": self._headless,
            "args": launch_args,
        }

        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
            # Don't log the full server URL - it contains embedded credentials!
            logger.info(f"🔐 PROXY ATTACHED TO chromium.launch() - host: {config.proxy_server_host}")
        else:
            logger.error("⚠️ NO PROXY ATTACHED - Browser launching with DIRECT CONNECTION!")
            logger.error("⚠️ Uber Eats WILL detect datacenter IP and return degraded page!")

        logger.info(f"Launching browser: headless={self._headless}, proxy={'ATTACHED (auth embedded)' if proxy_config else 'NONE'}")
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        logger.info("✅ Browser launched successfully")

        # Viewport with slight randomization
        viewport_width = 1920 + random.randint(-50, 50)
        viewport_height = 1080 + random.randint(-30, 30)

        context_kwargs = {
            "viewport": {"width": viewport_width, "height": viewport_height},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()

        # Block heavy resources
        await self._setup_resource_blocking(self._page)

        # ========================================================================
        # STEP 4: CONDITIONALLY APPLY STEALTH
        # ========================================================================
        if skip_stealth:
            logger.info("⚠️ STEALTH SKIPPED (skip_stealth=True) - May help with Uber Eats")
        elif config.stealth_mode:
            stealth_func = _get_stealth_async()
            if stealth_func:
                await stealth_func(self._page)
                logger.info("Stealth mode applied")

        self._page.set_default_timeout(30000)
        logger.info(f"Browser ready (viewport: {viewport_width}x{viewport_height})")

    # ========================================================================
    # STEP 1: OUTBOUND IP VERIFICATION (MANDATORY)
    # ========================================================================
    async def verify_outbound_ip(self) -> dict:
        """
        Navigate to ipify to verify the outbound IP address.

        This MUST be called before any target site navigation to prove proxy is working.
        Returns dict with ip, is_residential (heuristic), verification_success.
        """
        page = await self.ensure_browser()

        logger.info("=" * 70)
        logger.info("🌐 OUTBOUND IP VERIFICATION (via api.ipify.org)")
        logger.info("=" * 70)

        try:
            # Navigate to ipify
            response = await page.goto(
                "https://api.ipify.org?format=json",
                wait_until="domcontentloaded",
                timeout=15000
            )

            if not response or response.status != 200:
                logger.error(f"❌ IP CHECK FAILED: HTTP {response.status if response else 'no response'}")
                return {"ip": None, "verification_success": False, "error": "HTTP error"}

            # Get the page content (should be JSON)
            content = await page.content()

            # Parse IP from response
            import re
            ip_match = re.search(r'"ip"\s*:\s*"([^"]+)"', content)
            if ip_match:
                ip_address = ip_match.group(1)
                logger.info(f"🌐 OUTBOUND IP CHECK: {{'ip': '{ip_address}'}}")

                # Heuristic: Check if IP looks residential
                # Railway IPs are typically in specific ranges
                is_likely_datacenter = (
                    ip_address.startswith("34.") or   # GCP
                    ip_address.startswith("35.") or   # GCP
                    ip_address.startswith("104.") or  # Various cloud
                    ip_address.startswith("52.") or   # AWS
                    ip_address.startswith("54.") or   # AWS
                    ip_address.startswith("18.") or   # AWS
                    ip_address.startswith("3.") or    # AWS
                    ip_address.startswith("13.")      # AWS
                )

                if is_likely_datacenter:
                    logger.warning(f"⚠️ IP {ip_address} LOOKS LIKE DATACENTER - Proxy may not be working!")
                else:
                    logger.info(f"✅ IP {ip_address} does NOT match common datacenter ranges")

                return {
                    "ip": ip_address,
                    "is_likely_datacenter": is_likely_datacenter,
                    "verification_success": True
                }
            else:
                logger.error(f"❌ Could not parse IP from response: {content[:200]}")
                return {"ip": None, "verification_success": False, "error": "Parse error"}

        except Exception as e:
            logger.error(f"❌ IP VERIFICATION FAILED: {e}")
            return {"ip": None, "verification_success": False, "error": str(e)}
        finally:
            logger.info("=" * 70)

    async def _setup_resource_blocking(self, page) -> None:
        """Block fonts, images, media to prevent stalls on slow/blocked connections."""
        async def block_resources(route):
            resource_type = route.request.resource_type
            # Block: fonts, images, media, stylesheets (keep: document, script, xhr, fetch)
            if resource_type in ["font", "image", "media", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()

        try:
            await page.route("**/*", block_resources)
            logger.info("Resource blocking enabled: fonts, images, media, stylesheets blocked")
        except Exception as e:
            logger.warning(f"Could not setup resource blocking: {e}")

    async def _create_fresh_context(self) -> None:
        """Create a fresh browser context and page (for retry with clean state)."""
        # Close existing context/page if any
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        # Create new context with randomized viewport
        viewport_width = 1920 + random.randint(-50, 50)
        viewport_height = 1080 + random.randint(-30, 30)

        context_kwargs = {
            "viewport": {"width": viewport_width, "height": viewport_height},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()

        # Setup resource blocking on new page
        await self._setup_resource_blocking(self._page)

        # Apply stealth if available
        if self._config and self._config.stealth_mode:
            stealth_func = _get_stealth_async()
            if stealth_func:
                await stealth_func(self._page)

        self._page.set_default_timeout(30000)
        logger.info(f"Fresh context created with viewport: {viewport_width}x{viewport_height}")

    async def close(self) -> None:
        """Close the browser and cleanup."""
        if self._page:
            await self._page.close()
            self._page = None
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        logger.info("Browser closed")

    async def navigate(self, url: str, wait_until: str = "commit", timeout: int = 45000) -> Dict[str, Any]:
        """
        Navigate to a URL with robust retry logic.

        Strategy:
        1. Use wait_until="commit" to avoid hanging on SPA hydration
        2. Fresh context per retry to avoid poisoned sessions
        3. Readiness probe after navigation to detect degraded pages
        4. Resource blocking already applied to prevent font/image stalls
        """
        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            # Fresh context for each retry (avoids poisoned sessions)
            if attempt > 0:
                backoff = (2 ** attempt) + random.uniform(0.5, 1.5)
                logger.info(f"Nav retry {attempt + 1}/{max_retries} with fresh context after {backoff:.1f}s")
                await asyncio.sleep(backoff)
                try:
                    await self._create_fresh_context()
                except Exception as e:
                    logger.error(f"Fresh context failed: {e}")
                    continue

            page = await self.ensure_browser()

            # Set referrer for stealth
            try:
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
                await page.set_extra_http_headers({
                    "Referer": f"https://www.google.com/search?q={domain}"
                })
            except Exception:
                pass

            # Small human-like delay
            await asyncio.sleep(random.uniform(0.2, 0.5))

            try:
                # Navigate with commit-level wait (fastest, just waits for response)
                logger.info(f"Navigating to {url} (attempt {attempt + 1}, wait={wait_until}, timeout={timeout}ms)")
                response = await page.goto(url, wait_until=wait_until, timeout=timeout)

                # Brief pause for initial JS execution
                await asyncio.sleep(1.0)

                # READINESS PROBE: Check if page is alive with minimal selectors
                is_ready = await self._check_page_readiness(page)

                if not is_ready:
                    logger.warning(f"Attempt {attempt + 1}: Page not ready (degraded/blocked)")
                    last_error = "Page loaded but appears degraded or blocked"
                    continue  # Try fresh context

                # Auto-dismiss cookie banners
                cookie_dismissed = await self._try_dismiss_cookies(page)

                content = f"Navigated to {url}"
                if cookie_dismissed:
                    content += " (cookie banner dismissed)"

                logger.info(f"Navigation successful on attempt {attempt + 1}")
                return {
                    "success": True,
                    "content": content,
                    "status": response.status if response else None,
                    "cookie_dismissed": cookie_dismissed,
                    "attempts": attempt + 1,
                }

            except Exception as e:
                error_msg = str(e)
                last_error = error_msg
                logger.warning(f"Navigation attempt {attempt + 1} failed: {error_msg}")

                # On crash, we'll get fresh context on next iteration anyway
                if "crashed" in error_msg.lower() or "closed" in error_msg.lower():
                    logger.info("Page crashed - will use fresh context on retry")

        # All retries exhausted
        return {
            "success": False,
            "error": f"Navigation failed after {max_retries} attempts: {last_error}",
            "content": None,
            "attempts": max_retries,
        }

    async def _check_page_readiness(self, page, timeout_ms: int = 5000) -> bool:
        """
        Quick readiness probe - check if page has basic interactive elements.

        Returns True if page appears functional, False if degraded/blocked.
        """
        # Minimal selectors that should exist on ANY working webpage
        readiness_selectors = [
            "body",           # Most basic - page has body
            "a",              # Any link
            "button",         # Any button
            "input",          # Any input
            "[role]",         # Any ARIA role
        ]

        try:
            for selector in readiness_selectors:
                try:
                    element = await page.wait_for_selector(
                        selector,
                        timeout=timeout_ms,
                        state="attached"
                    )
                    if element:
                        logger.info(f"Readiness probe passed: found '{selector}'")
                        return True
                except Exception:
                    continue

            # No basic elements found
            logger.warning("Readiness probe failed: no basic elements found")
            return False

        except Exception as e:
            logger.warning(f"Readiness probe error: {e}")
            return False

    async def _try_dismiss_cookies(self, page) -> bool:
        """Try to dismiss cookie consent banners with common selectors."""
        # Common cookie consent button selectors (ordered by specificity)
        cookie_selectors = [
            # Greenhouse/OneTrust specific
            "button:has-text('Accept cookies')",
            "button:has-text('Accept Cookies')",
            "#onetrust-accept-btn-handler",
            ".onetrust-accept-btn-handler",
            # Generic accept buttons
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('I Accept')",
            "button:has-text('I agree')",
            "button:has-text('Agree')",
            "button:has-text('OK')",
            "button:has-text('Got it')",
            # Common class/id patterns
            "[data-testid='cookie-accept']",
            ".cookie-accept",
            ".accept-cookies",
            ".cookie-consent-accept",
            "#accept-cookies",
            "#cookie-accept",
            # Close buttons on cookie modals
            ".cookie-banner button",
            ".cookie-notice button",
            ".cookie-popup button",
            "[class*='cookie'] button:has-text('Accept')",
            "[class*='cookie'] button:has-text('Close')",
            "[id*='cookie'] button:has-text('Accept')",
        ]

        for selector in cookie_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible(timeout=500):
                    await locator.click(timeout=2000)
                    logger.info(f"Dismissed cookie banner with selector: {selector}")
                    # Wait briefly for banner to disappear
                    await asyncio.sleep(0.5)
                    return True
            except Exception:
                # Selector not found or not clickable, try next
                continue

        return False

    async def click(self, selector: str = None) -> Dict[str, Any]:
        """Click an element. If no selector provided, auto-detect clickable element."""
        page = await self.ensure_browser()

        # Add human-like delay before clicking
        await self._human_delay(100, 400)

        auto_selected = None
        if not selector:
            # Auto-detection logic: try common patterns
            # Greenhouse-specific selectors first
            auto_selectors = [
                # Greenhouse Apply button patterns
                ("a[href*='#app']", "Greenhouse Apply anchor"),
                ("a:has-text('Apply for this job')", "Apply for this job link"),
                ("a:has-text('Apply now')", "Apply now link"),
                ("a:has-text('Apply')", "Apply link"),
                ("button:has-text('Apply for this job')", "Apply button"),
                ("button:has-text('Apply now')", "Apply now button"),
                ("button:has-text('Apply')", "Apply button"),
                # Generic fallbacks
                (".opening a", "Job listing link"),
                ("a[href]", "First link"),
                ("button", "First button"),
            ]

            for sel, desc in auto_selectors:
                try:
                    locator = page.locator(sel).first
                    if await locator.count() > 0 and await locator.is_visible():
                        selector = sel
                        auto_selected = f"Auto-selected selector: {sel} ({desc})"
                        break
                except Exception:
                    continue

            if not selector:
                return {
                    "success": False,
                    "error": "No clickable element found (tried Apply button, links, buttons)",
                }

        await page.wait_for_selector(selector, state="visible", timeout=10000)
        await page.click(selector)

        content = f"Clicked {selector}"
        if auto_selected:
            content = f"{auto_selected}\n{content}"

        return {
            "success": True,
            "content": content,
            "auto_selected": auto_selected,
        }

    async def fill(self, selector: str, value: str) -> Dict[str, Any]:
        """Fill text into an input field."""
        page = await self.ensure_browser()
        await page.wait_for_selector(selector, state="visible", timeout=10000)
        await page.fill(selector, value)

        return {
            "success": True,
            "content": f"Filled {selector} with text",
        }

    async def type_text(self, selector: str, text: str) -> Dict[str, Any]:
        """Type text keystroke by keystroke with human-like typing speed."""
        page = await self.ensure_browser()
        await page.wait_for_selector(selector, state="visible", timeout=10000)

        # Human-like delay before typing
        await self._human_delay(50, 200)

        # Type with human-like speed (random delay per character: 50-150ms)
        typing_delay = random.randint(50, 120)
        await page.type(selector, text, delay=typing_delay)

        return {
            "success": True,
            "content": f"Typed into {selector}",
        }

    async def screenshot(self, timeout: int = 10000) -> Dict[str, Any]:
        """
        Take a screenshot and return as base64.

        Uses asyncio.wait_for to enforce timeout - avoids hanging on font loading.
        """
        page = await self.ensure_browser()

        try:
            # Wrap screenshot in a timeout to avoid hanging on font loading
            screenshot_coro = page.screenshot(type="jpeg", quality=80)
            screenshot_bytes = await asyncio.wait_for(screenshot_coro, timeout=timeout / 1000.0)
            screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            return {
                "success": True,
                "content": "Screenshot captured",
                "screenshot_base64": screenshot_base64,
            }
        except asyncio.TimeoutError:
            logger.warning(f"Screenshot timed out after {timeout}ms (likely font loading stall)")
            # Try a fallback: clip to viewport only, which is faster
            try:
                screenshot_bytes = await asyncio.wait_for(
                    page.screenshot(type="jpeg", quality=60, clip={"x": 0, "y": 0, "width": 1280, "height": 720}),
                    timeout=5.0
                )
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                return {
                    "success": True,
                    "content": "Screenshot captured (fallback viewport clip)",
                    "screenshot_base64": screenshot_base64,
                }
            except Exception:
                return {
                    "success": False,
                    "error": "Screenshot timed out even with fallback",
                    "content": None,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Screenshot failed: {str(e)}",
                "content": None,
            }

    async def screenshot_fast(self) -> Dict[str, Any]:
        """
        Take a fast screenshot with minimal waiting.

        Useful for debugging when page may be stalled.
        """
        return await self.screenshot(timeout=5000)

    async def probe_selector(self, selector: str, timeout: int = 3000) -> Dict[str, Any]:
        """
        Probe for a selector's existence without clicking.

        Returns whether the element exists and is visible.
        Useful for detecting page state (bot blocks, consent walls, etc.)
        """
        page = await self.ensure_browser()

        try:
            element = await page.wait_for_selector(selector, timeout=timeout, state="attached")
            if element:
                is_visible = await element.is_visible()
                return {
                    "success": True,
                    "exists": True,
                    "visible": is_visible,
                    "content": f"Selector '{selector}' found (visible={is_visible})",
                }
            return {
                "success": True,
                "exists": False,
                "visible": False,
                "content": f"Selector '{selector}' not found",
            }
        except Exception:
            return {
                "success": True,
                "exists": False,
                "visible": False,
                "content": f"Selector '{selector}' not found",
            }

    async def wait_for(self, selector: str = None, timeout: int = 30000) -> Dict[str, Any]:
        """Wait for an element or duration."""
        page = await self.ensure_browser()

        if selector:
            await page.wait_for_selector(selector, timeout=timeout)
            return {"success": True, "content": f"Element {selector} found"}
        else:
            await asyncio.sleep(timeout / 1000.0)
            return {"success": True, "content": f"Waited {timeout}ms"}

    async def scroll(self, direction: str = "down", amount: int = None) -> Dict[str, Any]:
        """Scroll the page by pixels."""
        page = await self.ensure_browser()

        scroll_amount = amount or 500
        if direction == "down":
            await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        elif direction == "up":
            await page.evaluate(f"window.scrollBy(0, -{scroll_amount})")

        return {
            "success": True,
            "content": f"Scrolled {direction} by {scroll_amount}px",
        }

    async def press_key(self, key: str) -> Dict[str, Any]:
        """Press a keyboard key (Enter, ArrowDown, Escape, etc.)."""
        page = await self.ensure_browser()

        try:
            await page.keyboard.press(key)
            return {
                "success": True,
                "content": f"Pressed key: {key}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to press key {key}: {str(e)}",
                "content": None,
            }

    async def scroll_to_element(self, selector: str) -> Dict[str, Any]:
        """Scroll to bring an element into view."""
        page = await self.ensure_browser()

        try:
            locator = page.locator(selector)
            await locator.scroll_into_view_if_needed(timeout=10000)
            return {
                "success": True,
                "content": f"Scrolled to element: {selector}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to scroll to element {selector}: {str(e)}",
            }

    async def scroll_until_text(self, text: str, max_scrolls: int = 10) -> Dict[str, Any]:
        """Scroll down until specific text is found on the page."""
        page = await self.ensure_browser()

        for i in range(max_scrolls):
            # Check if text exists on page
            content = await page.content()
            if text.lower() in content.lower():
                return {
                    "success": True,
                    "content": f"Found text '{text}' after {i} scrolls",
                }

            # Scroll down
            await page.evaluate("window.scrollBy(0, 500)")
            await asyncio.sleep(0.5)  # Wait for content to load

        return {
            "success": False,
            "error": f"Text '{text}' not found after {max_scrolls} scrolls",
        }

    async def file_upload(self, selector: str, paths: list) -> Dict[str, Any]:
        """Upload files to a file input."""
        page = await self.ensure_browser()
        await page.set_input_files(selector, paths)

        return {
            "success": True,
            "content": f"Uploaded {len(paths)} file(s)",
        }

    async def get_content(self, selector: str = None) -> Dict[str, Any]:
        """Get page or element content."""
        page = await self.ensure_browser()

        if selector:
            element = await page.query_selector(selector)
            if element:
                content = await element.text_content()
                return {"success": True, "content": content}
            return {"success": False, "error": f"Element {selector} not found"}

        content = await page.content()
        return {"success": True, "content": content[:1000] + "..." if len(content) > 1000 else content}

    async def get_elements_with_boxes(self) -> Dict[str, Any]:
        """Extract clickable elements with bounding boxes for visual picker."""
        page = await self.ensure_browser()

        # JavaScript to extract clickable elements
        elements = await page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();

                // Selectors for clickable/interactive elements
                const selectors = [
                    'a[href]', 'button', 'input', 'select', 'textarea',
                    '[onclick]', '[role="button"]', '[role="link"]',
                    'label', '.btn', '[type="submit"]'
                ];

                // Helper to generate a unique CSS selector
                function getSelector(el) {
                    if (el.id) return '#' + CSS.escape(el.id);
                    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';

                    // Use data-testid if available
                    if (el.dataset && el.dataset.testid) return '[data-testid="' + el.dataset.testid + '"]';

                    // Use unique class if available
                    const classes = Array.from(el.classList || []).filter(c => {
                        try {
                            return document.querySelectorAll('.' + CSS.escape(c)).length === 1;
                        } catch { return false; }
                    });
                    if (classes.length) return '.' + CSS.escape(classes[0]);

                    // Fallback: tag + nth-of-type
                    const parent = el.parentElement;
                    if (!parent) return el.tagName.toLowerCase();
                    const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
                    const index = siblings.indexOf(el) + 1;
                    return el.tagName.toLowerCase() + ':nth-of-type(' + index + ')';
                }

                for (const selector of selectors) {
                    try {
                        document.querySelectorAll(selector).forEach(el => {
                            // Skip hidden elements
                            if (el.offsetParent === null && el.tagName !== 'BODY') return;

                            const rect = el.getBoundingClientRect();
                            // Skip elements with no size or outside viewport
                            if (rect.width < 5 || rect.height < 5) return;
                            if (rect.bottom < 0 || rect.top > window.innerHeight) return;
                            if (rect.right < 0 || rect.left > window.innerWidth) return;

                            // Dedupe by position
                            const key = Math.round(rect.x) + ',' + Math.round(rect.y);
                            if (seen.has(key)) return;
                            seen.add(key);

                            results.push({
                                selector: getSelector(el),
                                tag: el.tagName.toLowerCase(),
                                text: (el.textContent || '').trim().substring(0, 50),
                                placeholder: el.placeholder || null,
                                bbox: {
                                    x: Math.round(rect.x),
                                    y: Math.round(rect.y),
                                    width: Math.round(rect.width),
                                    height: Math.round(rect.height)
                                }
                            });
                        });
                    } catch (e) {
                        // Skip selector errors
                    }
                }

                return results;
            }
        """)

        return {
            "success": True,
            "elements": elements,
            "count": len(elements)
        }

    async def wait(self, duration_ms: int) -> Dict[str, Any]:
        """Wait for a specified duration in milliseconds."""
        await asyncio.sleep(duration_ms / 1000.0)
        return {
            "success": True,
            "content": f"Waited {duration_ms}ms"
        }

    async def get_current_url(self) -> Dict[str, Any]:
        """Get the current page URL."""
        page = await self.ensure_browser()
        url = page.url
        return {
            "success": True,
            "content": url,
            "url": url
        }

    async def get_element_count(self, selector: str) -> Dict[str, Any]:
        """Get the count of elements matching a selector."""
        page = await self.ensure_browser()
        try:
            locator = page.locator(selector)
            count = await locator.count()
            return {
                "success": True,
                "content": f"Found {count} elements matching {selector}",
                "count": count
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to count elements: {str(e)}",
                "count": 0
            }

    async def click_first_job(self) -> Dict[str, Any]:
        """
        Detect if on a Greenhouse index page and click the first job link.

        This action is designed to handle the case where a job URL redirects
        to an index page (e.g., job closed). It detects the index page and
        clicks the first job listing.

        Returns:
            Dict with success status and info about what was clicked.
        """
        page = await self.ensure_browser()
        current_url = page.url

        # Check if we're already on a job detail page (URL contains /jobs/)
        if "/jobs/" in current_url and not current_url.endswith("/jobs") and not current_url.endswith("/jobs/"):
            return {
                "success": True,
                "content": f"Already on job detail page: {current_url}",
                "skipped": True,
                "url": current_url
            }

        # Try Greenhouse-specific job listing selectors
        job_link_selectors = [
            ".opening a",           # Greenhouse standard
            "a.opening",            # Alternative structure
            "a[href*='/jobs/']",    # Any link to a job
            ".job-listing a",       # Common pattern
            ".job-post a",          # Another common pattern
        ]

        for selector in job_link_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible(timeout=2000):
                    # Get the href before clicking
                    href = await locator.get_attribute("href")
                    job_title = await locator.text_content()

                    # Click the first job
                    await locator.click()

                    # Wait for navigation
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(1)  # Extra wait for page to settle

                    new_url = page.url

                    return {
                        "success": True,
                        "content": f"Clicked first job: {job_title.strip() if job_title else href}",
                        "selector_used": selector,
                        "job_href": href,
                        "job_title": job_title.strip() if job_title else None,
                        "new_url": new_url,
                        "skipped": False
                    }
            except Exception:
                continue

        # No job links found - might already be on detail page or no jobs available
        return {
            "success": False,
            "error": f"No job listings found on page. Current URL: {current_url}",
            "url": current_url
        }

    async def extract_job_links(self) -> Dict[str, Any]:
        """Extract all job posting links from a Greenhouse board page."""
        page = await self.ensure_browser()

        logger.info("Extracting job links from Greenhouse board")

        # Greenhouse-specific selectors for job links
        job_selectors = [
            ".opening a",           # Classic Greenhouse
            "a.opening",            # Alternative
            "a[href*='/jobs/']",    # URL-based
            "[data-mapped='true'] a",  # Some newer boards
            ".job-post a",          # Yet another variant
            ".job-listing a",       # Common pattern
        ]

        job_urls = []
        seen_urls = set()
        base_url = "/".join(page.url.split("/")[:3])  # e.g., https://boards.greenhouse.io

        for selector in job_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href")
                    if href and href not in seen_urls:
                        # Normalize relative URLs
                        if href.startswith("/"):
                            href = base_url + href

                        # Filter to job URLs only
                        if "/jobs/" in href or "/job/" in href:
                            seen_urls.add(href)
                            job_urls.append(href)

            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue

        logger.info(f"Found {len(job_urls)} job links")

        return {
            "success": True,
            "content": f"Extracted {len(job_urls)} job links",
            "extracted_data": job_urls,
        }

    async def extract(self, selector: str = None, extract_mode: str = "text", attribute: str = None) -> Dict[str, Any]:
        """Extract text or attribute from elements on the page."""
        page = await self.ensure_browser()

        try:
            if selector:
                # Extract from specific elements
                locator = page.locator(selector)
                count = await locator.count()

                if count == 0:
                    return {
                        "success": False,
                        "error": f"No elements found for selector: {selector}",
                        "extracted_data": None
                    }

                if extract_mode == "attribute" and attribute:
                    # Extract attribute from all matching elements
                    extracted = []
                    for i in range(count):
                        val = await locator.nth(i).get_attribute(attribute)
                        if val:
                            extracted.append(val)
                    return {
                        "success": True,
                        "content": f"Extracted '{attribute}' attribute from {len(extracted)} elements",
                        "extracted_data": extracted
                    }
                else:
                    # Extract inner text from all matching elements
                    extracted = await locator.all_inner_texts()
                    # Filter out empty strings
                    extracted = [t.strip() for t in extracted if t.strip()]
                    return {
                        "success": True,
                        "content": f"Extracted text from {len(extracted)} elements",
                        "extracted_data": extracted
                    }
            else:
                # Extract full page text
                body_text = await page.inner_text("body")
                return {
                    "success": True,
                    "content": "Extracted full page text",
                    "extracted_data": body_text[:5000] if len(body_text) > 5000 else body_text
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"Extract failed: {str(e)}",
                "extracted_data": None
            }

    # ==========================================
    # Phase 7: Hard-Site Scraping Methods
    # ==========================================

    async def extract_links(
        self,
        selector: str,
        filter_pattern: str = None,
        include_text: bool = True
    ) -> Dict[str, Any]:
        """Extract all links matching selector with optional URL filtering."""
        import re
        page = await self.ensure_browser()

        try:
            locator = page.locator(selector)
            count = await locator.count()

            if count == 0:
                return {
                    "success": False,
                    "error": f"No elements found for selector: {selector}",
                    "extracted_data": None
                }

            links = []
            urls = []
            base_url = page.url

            for i in range(count):
                el = locator.nth(i)
                href = await el.get_attribute("href")

                if not href:
                    continue

                # Convert relative URLs to absolute
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(base_url, href)
                elif not href.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    href = urljoin(base_url, href)

                # Apply filter pattern if provided
                if filter_pattern:
                    if not re.search(filter_pattern, href):
                        continue

                # Deduplicate
                if href in urls:
                    continue

                urls.append(href)

                if include_text:
                    text = await el.inner_text()
                    links.append({"href": href, "text": text.strip()})
                else:
                    links.append({"href": href})

            return {
                "success": True,
                "content": f"Extracted {len(urls)} links from {count} elements",
                "extracted_data": {"urls": urls, "links": links}
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"extract_links failed: {str(e)}",
                "extracted_data": None
            }

    async def extract_text(
        self,
        selector: str,
        clean_whitespace: bool = True,
        max_length: int = None
    ) -> Dict[str, Any]:
        """Extract text content with optional cleaning and truncation."""
        import re
        page = await self.ensure_browser()

        try:
            locator = page.locator(selector)
            count = await locator.count()

            if count == 0:
                return {
                    "success": False,
                    "error": f"No elements found for selector: {selector}",
                    "extracted_data": None
                }

            extracted = []
            for i in range(count):
                text = await locator.nth(i).inner_text()

                if clean_whitespace:
                    # Collapse multiple whitespace chars to single space
                    text = re.sub(r'\s+', ' ', text).strip()

                if max_length and len(text) > max_length:
                    text = text[:max_length] + "..."

                if text:
                    extracted.append(text)

            return {
                "success": True,
                "content": f"Extracted text from {len(extracted)} elements",
                "extracted_data": extracted
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"extract_text failed: {str(e)}",
                "extracted_data": None
            }

    async def extract_attributes(
        self,
        selector: str,
        attributes: List[str]
    ) -> Dict[str, Any]:
        """Extract multiple attributes from elements."""
        page = await self.ensure_browser()

        try:
            locator = page.locator(selector)
            count = await locator.count()

            if count == 0:
                return {
                    "success": False,
                    "error": f"No elements found for selector: {selector}",
                    "extracted_data": None
                }

            extracted = []
            for i in range(count):
                el = locator.nth(i)
                attrs = {}
                for attr in attributes:
                    val = await el.get_attribute(attr)
                    attrs[attr] = val
                extracted.append(attrs)

            return {
                "success": True,
                "content": f"Extracted {len(attributes)} attributes from {len(extracted)} elements",
                "extracted_data": extracted
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"extract_attributes failed: {str(e)}",
                "extracted_data": None
            }

    async def scroll_until(
        self,
        condition: str = "count",
        selector: str = None,
        max_scrolls: int = 20,
        scroll_delay_ms: int = None
    ) -> Dict[str, Any]:
        """Scroll until a condition is met (selector_visible, end_of_page, count)."""
        page = await self.ensure_browser()

        scrolls_done = 0
        last_height = 0

        try:
            for i in range(max_scrolls):
                # Jittered delay between scrolls
                delay_ms = scroll_delay_ms or random.randint(500, 1500)
                await asyncio.sleep(delay_ms / 1000)

                if condition == "selector_visible":
                    if selector:
                        try:
                            is_visible = await page.locator(selector).is_visible()
                            if is_visible:
                                return {
                                    "success": True,
                                    "content": f"Selector visible after {i+1} scrolls",
                                    "scrolls_done": i + 1
                                }
                        except:
                            pass

                elif condition == "end_of_page":
                    current_height = await page.evaluate("document.body.scrollHeight")
                    if current_height == last_height:
                        return {
                            "success": True,
                            "content": f"Reached end of page after {i+1} scrolls",
                            "scrolls_done": i + 1
                        }
                    last_height = current_height

                # Perform scroll with random amount
                scroll_amount = random.randint(400, 700)
                await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                scrolls_done = i + 1

            return {
                "success": True,
                "content": f"Completed {scrolls_done} scrolls (max reached)",
                "scrolls_done": scrolls_done
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"scroll_until failed: {str(e)}",
                "scrolls_done": scrolls_done
            }

    async def random_scroll(
        self,
        min_scrolls: int = 2,
        max_scrolls: int = 5,
        min_delay_ms: int = 300,
        max_delay_ms: int = 1200,
        direction: str = "down"
    ) -> Dict[str, Any]:
        """Human-like scrolling with randomized amounts and timing."""
        page = await self.ensure_browser()

        num_scrolls = random.randint(min_scrolls, max_scrolls)
        scrolls_done = 0

        try:
            for i in range(num_scrolls):
                # Random delay before scroll
                delay = random.randint(min_delay_ms, max_delay_ms) / 1000
                await asyncio.sleep(delay)

                # Random scroll amount
                amount = random.randint(200, 600)

                # Direction
                if direction == "random":
                    actual_direction = random.choice(["up", "down"])
                else:
                    actual_direction = direction

                scroll_amount = amount if actual_direction == "down" else -amount
                await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                scrolls_done += 1

            return {
                "success": True,
                "content": f"Completed {scrolls_done} random scrolls",
                "scrolls_done": scrolls_done
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"random_scroll failed: {str(e)}",
                "scrolls_done": scrolls_done
            }

    async def detect_block(self) -> Dict[str, Any]:
        """Detect if page shows bot-detection patterns (CAPTCHA, access denied, etc.)."""
        page = await self.ensure_browser()

        try:
            page_content = await page.content()
            page_text = await page.inner_text("body")
            page_url = page.url

            indicators = []
            block_type = None

            # CAPTCHA detection patterns
            captcha_patterns = [
                ("recaptcha", "iframe[src*='recaptcha']", "reCAPTCHA"),
                ("hcaptcha", "iframe[src*='hcaptcha']", "hCaptcha"),
                ("cloudflare", ".cf-browser-verification", "Cloudflare"),
                ("turnstile", "iframe[src*='turnstile']", "Cloudflare Turnstile"),
            ]

            for name, selector, display_name in captcha_patterns:
                try:
                    if await page.locator(selector).count() > 0:
                        indicators.append(f"{display_name} detected")
                        block_type = name
                except:
                    pass

            # Text-based detection patterns
            block_text_patterns = [
                ("access_denied", ["access denied", "access blocked", "you have been blocked"]),
                ("rate_limited", ["rate limit", "too many requests", "try again later"]),
                ("bot_detection", ["unusual traffic", "automated access", "are you a robot", "prove you're human"]),
                ("login_wall", ["please sign in", "please log in", "login required"]),
            ]

            lower_text = page_text.lower()
            for name, patterns in block_text_patterns:
                for pattern in patterns:
                    if pattern in lower_text:
                        indicators.append(f"Text pattern: '{pattern}'")
                        if not block_type:
                            block_type = name
                        break

            # Check for Cloudflare challenge page
            if "checking your browser" in lower_text or "just a moment" in lower_text:
                indicators.append("Cloudflare challenge page")
                block_type = "cloudflare_challenge"

            blocked = len(indicators) > 0

            return {
                "success": True,
                "content": f"Block detection complete: {'BLOCKED' if blocked else 'OK'}",
                "blocked": blocked,
                "block_type": block_type,
                "indicators": indicators
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"detect_block failed: {str(e)}",
                "blocked": False,
                "block_type": None,
                "indicators": []
            }

    async def wait_for_selector_with_fallbacks(
        self,
        selector: str,
        fallback_selectors: List[str] = None,
        timeout_ms: int = 10000,
        state: str = "visible"
    ) -> Dict[str, Any]:
        """Wait for selector with fallback chain."""
        page = await self.ensure_browser()

        selectors_to_try = [selector] + (fallback_selectors or [])
        tried = []

        for sel in selectors_to_try:
            try:
                await page.wait_for_selector(sel, state=state, timeout=timeout_ms)
                return {
                    "success": True,
                    "content": f"Found selector: {sel}",
                    "matched_selector": sel,
                    "tried": tried
                }
            except Exception as e:
                tried.append({"selector": sel, "error": str(e)})
                continue

        return {
            "success": False,
            "error": f"No selectors matched after trying {len(tried)} options",
            "matched_selector": None,
            "tried": tried
        }


# Global runtime instance
_runtime: Optional[PlaywrightRuntime] = None


async def get_runtime() -> PlaywrightRuntime:
    """Get or create the Playwright runtime."""
    global _runtime
    if _runtime is None:
        _runtime = PlaywrightRuntime()
    return _runtime


async def shutdown_runtime() -> None:
    """Shutdown the Playwright runtime."""
    global _runtime
    if _runtime:
        await _runtime.close()
        _runtime = None


async def execute_mcp_tool(tool_name: str, arguments: Dict[str, Any]) -> "MCPToolResult":
    """
    Execute an MCP tool using the Playwright runtime.

    This function maps MCP tool names to Playwright runtime methods.
    """
    from .mcp_client import MCPToolResult

    runtime = await get_runtime()

    try:
        # Map tool names to runtime methods
        tool_mapping = {
            "browser_navigate": lambda: runtime.navigate(arguments.get("url", "")),
            "browser_click": lambda: runtime.click(arguments.get("selector") or None),
            "browser_fill": lambda: runtime.fill(
                arguments.get("selector", ""),
                arguments.get("value", "")
            ),
            "browser_type": lambda: runtime.type_text(
                arguments.get("selector", ""),
                arguments.get("text", "")
            ),
            "browser_screenshot": lambda: runtime.screenshot(arguments.get("timeout", 10000)),
            "browser_screenshot_fast": lambda: runtime.screenshot_fast(),
            "browser_probe_selector": lambda: runtime.probe_selector(
                arguments.get("selector", ""),
                arguments.get("timeout", 3000)
            ),
            "browser_wait_for": lambda: runtime.wait_for(
                arguments.get("selector"),
                arguments.get("timeout", 30000)
            ),
            "browser_scroll": lambda: runtime.scroll(
                arguments.get("direction", "down"),
                arguments.get("amount")
            ),
            "browser_press_key": lambda: runtime.press_key(
                arguments.get("key", "Enter")
            ),
            "browser_file_upload": lambda: runtime.file_upload(
                arguments.get("selector", ""),
                arguments.get("paths", [])
            ),
            "browser_get_content": lambda: runtime.get_content(arguments.get("selector")),
            "browser_extract": lambda: runtime.extract(
                arguments.get("selector"),
                arguments.get("extract_mode", "text"),
                arguments.get("attribute")
            ),
            "browser_close": lambda: runtime.close(),
            "browser_get_current_url": lambda: runtime.get_current_url(),
            "browser_get_element_count": lambda: runtime.get_element_count(
                arguments.get("selector", "")
            ),
            "browser_click_first_job": lambda: runtime.click_first_job(),
            "browser_scroll_to_element": lambda: runtime.scroll_to_element(
                arguments.get("selector", "")
            ),
            "browser_scroll_until_text": lambda: runtime.scroll_until_text(
                arguments.get("text", ""),
                arguments.get("max_scrolls", 10)
            ),
            "browser_extract_job_links": lambda: runtime.extract_job_links(),
            # Phase 7: Hard-Site Scraping tools
            "browser_extract_links": lambda: runtime.extract_links(
                arguments.get("selector", "a"),
                arguments.get("filter_pattern"),
                arguments.get("include_text", True)
            ),
            "browser_extract_text": lambda: runtime.extract_text(
                arguments.get("selector", ""),
                arguments.get("clean_whitespace", True),
                arguments.get("max_length")
            ),
            "browser_extract_attributes": lambda: runtime.extract_attributes(
                arguments.get("selector", ""),
                arguments.get("attributes", [])
            ),
            "browser_scroll_until": lambda: runtime.scroll_until(
                arguments.get("condition", "count"),
                arguments.get("selector"),
                arguments.get("max_scrolls", 20),
                arguments.get("scroll_delay_ms")
            ),
            "browser_random_scroll": lambda: runtime.random_scroll(
                arguments.get("min_scrolls", 2),
                arguments.get("max_scrolls", 5),
                arguments.get("min_delay_ms", 300),
                arguments.get("max_delay_ms", 1200),
                arguments.get("direction", "down")
            ),
            "browser_detect_block": lambda: runtime.detect_block(),
            "browser_wait_for_selector": lambda: runtime.wait_for_selector_with_fallbacks(
                arguments.get("selector", ""),
                arguments.get("fallback_selectors"),
                arguments.get("timeout_ms", 10000),
                arguments.get("state", "visible")
            ),
        }

        if tool_name not in tool_mapping:
            return MCPToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}"
            )

        result = await tool_mapping[tool_name]()

        return MCPToolResult(
            success=result.get("success", True),
            content=result.get("content"),
            error=result.get("error"),
            screenshot_base64=result.get("screenshot_base64"),
            extracted_data=result.get("extracted_data"),
        )

    except Exception as e:
        logger.error(f"Tool execution failed: {tool_name} - {e}")
        return MCPToolResult(
            success=False,
            error=str(e)
        )
