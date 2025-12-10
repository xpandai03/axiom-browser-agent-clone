"""
MCP Runtime for Playwright browser automation.

This module provides the actual browser automation using Playwright,
designed to work with Cursor/Claude's native MCP integration.

When running in Cursor with MCP configured:
- Cursor manages the Playwright MCP server lifecycle
- This runtime executes browser commands via Playwright
- Screenshots are captured and returned as base64

The runtime can operate in two modes:
1. Native MCP mode: Tools called through Cursor's MCP protocol
2. Direct Playwright mode: Fallback for standalone execution
"""

import asyncio
import base64
import logging
import os
from typing import Any, Dict, Optional
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Playwright imports - optional for environments without it
try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not available - using simulation mode")


class PlaywrightRuntime:
    """
    Playwright runtime for browser automation.

    This class manages a Playwright browser instance and provides
    methods that map to MCP tool calls.
    """

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._headless = os.environ.get("BROWSER_HEADLESS", "true").lower() == "true"

    async def ensure_browser(self) -> Page:
        """Ensure browser is running and return the page."""
        if self._page is None:
            await self._start_browser()
        return self._page

    async def _start_browser(self) -> None:
        """Start the Playwright browser."""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not installed")

        logger.info(f"Starting Playwright browser (headless={self._headless})")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 720}
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(30000)

        logger.info("Browser started successfully")

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

    async def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate to a URL and auto-dismiss cookie banners."""
        page = await self.ensure_browser()
        response = await page.goto(url, wait_until="domcontentloaded")

        # Auto-dismiss cookie banners
        cookie_dismissed = await self._try_dismiss_cookies(page)

        content = f"Navigated to {url}"
        if cookie_dismissed:
            content += " (cookie banner dismissed)"

        return {
            "success": True,
            "content": content,
            "status": response.status if response else None,
            "cookie_dismissed": cookie_dismissed,
        }

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
        """Type text keystroke by keystroke."""
        page = await self.ensure_browser()
        await page.wait_for_selector(selector, state="visible", timeout=10000)
        await page.type(selector, text)

        return {
            "success": True,
            "content": f"Typed into {selector}",
        }

    async def screenshot(self) -> Dict[str, Any]:
        """Take a screenshot and return as base64."""
        page = await self.ensure_browser()
        screenshot_bytes = await page.screenshot(type="jpeg", quality=80)
        screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        return {
            "success": True,
            "content": "Screenshot captured",
            "screenshot_base64": screenshot_base64,
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
            "browser_screenshot": lambda: runtime.screenshot(),
            "browser_wait_for": lambda: runtime.wait_for(
                arguments.get("selector"),
                arguments.get("timeout", 30000)
            ),
            "browser_scroll": lambda: runtime.scroll(
                arguments.get("direction", "down"),
                arguments.get("amount")
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
