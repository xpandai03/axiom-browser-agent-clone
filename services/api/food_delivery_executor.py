"""
Food Delivery Workflow Executor for Uber Eats High-Protein Meal Finder.

Orchestrates the full browser automation workflow:
1. Navigate to Uber Eats and set delivery location
2. Search for high-protein restaurants
3. Extract menu items from each restaurant
4. Process nutrition data and estimate missing values
5. Assemble valid carts meeting protein/price constraints
6. Rank and return top 3 cart options

IMPORTANT: This executor does NOT use browser_evaluate.
All operations use: navigate, click, fill, type, screenshot, probe_selector, extract.
"""

import asyncio
import logging
import time
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime

from shared.schemas.food_delivery import (
    FoodDeliveryInput,
    FoodDeliveryOutput,
    FoodDeliveryConstraints,
    FoodDeliveryMetadata,
    DebugInfo,
    ExtractedMenuItem,
    ExtractedRestaurant,
    FailureReason,
)
from shared.schemas.workflow import WorkflowStep
from services.api.food_delivery_helpers import (
    process_extracted_item,
    parse_price,
    parse_nutrition_text,
    aggregate_all_carts,
    rank_carts,
    find_best_attempt,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Uber Eats Selectors (organized by page state)
# ============================================================================

SELECTORS = {
    # -------------------------------------------------------------------------
    # Bot/Geo/Consent Detection - check these first after navigation
    # -------------------------------------------------------------------------
    "bot_challenge": [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        ".cf-browser-verification",
        "#challenge-running",
        "[data-testid='challenge']",
        "div[class*='captcha']",
    ],
    "geo_selector": [
        "[data-testid='country-selector']",
        "[data-testid='location-selector-country']",
        "select[name='country']",
        "[aria-label*='country']",
        ".country-selector",
    ],
    "consent_wall": [
        "[data-testid='cookie-banner']",
        "#onetrust-banner-sdk",
        ".cookie-consent",
        "[aria-label*='cookie']",
        ".gdpr-banner",
    ],
    "age_gate": [
        "[data-testid='age-gate']",
        ".age-verification",
        "[aria-label*='age']",
    ],

    # -------------------------------------------------------------------------
    # Location/Address - multiple variants for different Uber Eats layouts
    # -------------------------------------------------------------------------
    "address_reveal_button": [
        # Button that reveals address input modal/dropdown
        "[data-testid='location-selector']",
        "button[aria-label*='delivery']",
        "button[aria-label*='Deliver']",
        "[data-testid='location-typeahead-trigger']",
        "button:has-text('Deliver to')",
        "[data-testid='address-container'] button",
        "header button:has-text('Enter')",
        # Mobile/compact layouts
        "[data-testid='header-address-trigger']",
    ],
    "address_input": [
        "input[data-testid='location-typeahead-input']",
        "input[placeholder*='Enter delivery address']",
        "input[placeholder*='address']",
        "input[aria-label*='delivery address']",
        "input[aria-label*='Address']",
        "#location-typeahead-input",
        "input[data-testid='address-input']",
        "[data-testid='location-typeahead'] input",
        # Generic fallbacks
        "input[type='text'][placeholder*='address']",
    ],
    "address_suggestion": [
        "[data-testid='location-typeahead-suggestion']",
        "[data-testid='address-suggestion']",
        "[role='option']",
        "li[role='option']",
        "[data-testid*='suggestion']",
        "[data-testid*='result']",
        ".autocomplete-item",
        ".suggestion-item",
        "[role='listbox'] li",
        "[role='listbox'] > div",
    ],
    "address_confirm": [
        "button[data-testid='location-typeahead-confirm']",
        "button:has-text('Done')",
        "button:has-text('Save')",
        "button:has-text('Confirm')",
        "[data-testid='confirm-location-button']",
    ],
    "address_display": [
        # Confirms address is set - look for truncated address text
        "[data-testid='location-selector'] span",
        "[data-testid='header-address']",
        "header [data-testid*='address']",
    ],

    # -------------------------------------------------------------------------
    # Search
    # -------------------------------------------------------------------------
    "search_input": [
        "input[data-testid='search-input']",
        "input[placeholder*='Search']",
        "input[aria-label*='Search']",
        "[data-testid='search-bar'] input",
    ],

    # -------------------------------------------------------------------------
    # Restaurant results
    # -------------------------------------------------------------------------
    "restaurant_card": [
        "[data-testid='store-card']",
        "a[href*='/store/']",
        "[data-testid='feed-item']",
        ".restaurant-card",
    ],
    "restaurant_name": [
        "[data-testid='store-name']",
        "h3",
        ".store-name",
    ],
    "restaurant_eta": [
        "[data-testid='store-eta']",
        "span:has-text('min')",
        ".delivery-time",
    ],

    # -------------------------------------------------------------------------
    # Menu items
    # -------------------------------------------------------------------------
    "menu_item_card": [
        "[data-testid='menu-item']",
        "[data-testid='store-item']",
        "[data-testid='item-card']",
        ".menu-item",
    ],
    "item_modal": [
        "[data-testid='item-modal']",
        "[role='dialog']",
        ".modal-content",
    ],
    "modal_close": [
        "[data-testid='modal-close']",
        "button[aria-label='Close']",
        "button[aria-label='close']",
        ".close-button",
        "[data-testid='close-button']",
    ],
}


class FoodDeliveryExecutor:
    """
    Executes the food delivery workflow using browser automation.

    This executor does NOT use browser_evaluate - all operations are done
    through the available MCP tools: navigate, click, fill, type, screenshot,
    probe_selector, extract, wait_for.
    """

    def __init__(self, mcp_client, headless: bool = True):
        self.client = mcp_client
        self.headless = headless
        self.debug = DebugInfo()
        self._workflow_context: dict = {}
        self._start_time: float = 0
        self._debug_screenshots: List[Dict[str, str]] = []

    async def execute(self, input_config: FoodDeliveryInput) -> FoodDeliveryOutput:
        """Execute the full food delivery workflow."""
        self._start_time = time.time()
        constraints = FoodDeliveryConstraints(
            min_protein_grams=input_config.min_protein_grams,
            max_price_usd=input_config.max_price_usd
        )

        try:
            # Phase A: Setup - Navigate and set location
            logger.info("=" * 70)
            logger.info("PHASE A: SETUP - Navigate and set delivery location")
            logger.info("=" * 70)

            setup_result = await self._setup_location(input_config.delivery_address)
            if setup_result != "success":
                return self._failure_output(input_config, constraints, setup_result)

            # Phase B: Search - Find restaurants
            logger.info("=" * 70)
            logger.info("PHASE B: SEARCH - Find high-protein restaurants")
            logger.info("=" * 70)

            restaurant_urls = await self._search_restaurants(
                input_config.search_terms,
                input_config.max_restaurants
            )
            if not restaurant_urls:
                return self._failure_output(
                    input_config, constraints, "no_restaurants_found_for_search_terms"
                )

            # Phase C: Extract - Scan menus
            logger.info("=" * 70)
            logger.info(f"PHASE C: EXTRACT - Scanning {len(restaurant_urls)} restaurants")
            logger.info("=" * 70)

            restaurants = await self._extract_restaurants(
                restaurant_urls,
                input_config.max_items_per_restaurant
            )

            # Check for bot detection
            if self.debug.blocked_at_step:
                return self._failure_output(
                    input_config, constraints, "bot_detection_triggered"
                )

            # Phase D: Process - Assemble and rank carts
            logger.info("=" * 70)
            logger.info("PHASE D: PROCESS - Assemble and rank carts")
            logger.info("=" * 70)

            all_carts, total_items, items_with_protein = aggregate_all_carts(
                restaurants, constraints
            )

            self.debug.items_extracted = total_items
            self.debug.items_with_protein_data = items_with_protein
            self.debug.items_with_estimates = sum(
                1 for r in restaurants for i in r.items
                if i.protein_source == "estimated"
            )
            self.debug.candidate_carts_generated = len(all_carts)

            if not all_carts:
                self.debug.best_attempt = find_best_attempt(
                    restaurants,
                    constraints.min_protein_grams,
                    constraints.max_price_usd
                )
                return self._failure_output(
                    input_config, constraints, "no_carts_meet_protein_and_price_constraints"
                )

            # Rank and select top 3
            ranked_results = rank_carts(
                all_carts,
                constraints.min_protein_grams,
                constraints.max_price_usd,
                top_n=3
            )

            self.debug.valid_carts_found = len(ranked_results)

            # Build success response
            duration_ms = int((time.time() - self._start_time) * 1000)
            metadata = FoodDeliveryMetadata(
                restaurants_scanned=self.debug.restaurants_scanned,
                items_extracted=self.debug.items_extracted,
                workflow_duration_ms=duration_ms,
                search_terms_used=input_config.search_terms
            )

            return FoodDeliveryOutput.success(
                location=input_config.delivery_address,
                constraints=constraints,
                search_terms=input_config.search_terms,
                results=ranked_results,
                metadata=metadata,
                debug=self.debug
            )

        except Exception as e:
            logger.exception(f"Workflow execution failed: {e}")
            return self._failure_output(input_config, constraints, "unknown_error")

    # ========================================================================
    # Debug Utilities
    # ========================================================================

    async def _take_debug_screenshot(self, label: str) -> Optional[str]:
        """Take a fast screenshot for debugging. Returns base64 or None."""
        try:
            result = await self.client.call_tool("browser_screenshot_fast", {})
            if result.success and result.screenshot_base64:
                self._debug_screenshots.append({
                    "label": label,
                    "base64": result.screenshot_base64
                })
                logger.info(f"📸 [{label}] Screenshot captured")
                return result.screenshot_base64
            else:
                logger.warning(f"📸 [{label}] Failed: {result.error if hasattr(result, 'error') else 'unknown'}")
        except Exception as e:
            logger.warning(f"📸 [{label}] Error: {e}")
        return None

    async def _probe_selector(self, selector: str, timeout: int = 3000) -> bool:
        """Probe if a selector exists and is visible."""
        try:
            result = await self.client.call_tool("browser_probe_selector", {
                "selector": selector,
                "timeout": timeout
            })
            return result.success and getattr(result, 'exists', False)
        except Exception:
            return False

    async def _verify_outbound_ip(self) -> dict:
        """
        Verify outbound IP by navigating to ipify.

        This is the executor-level wrapper that uses the MCP client.
        """
        import re

        logger.info("=" * 70)
        logger.info("🌐 OUTBOUND IP VERIFICATION (via api.ipify.org)")
        logger.info("=" * 70)

        try:
            # Navigate to ipify
            nav_result = await self.client.navigate("https://api.ipify.org?format=json")

            if not nav_result.success:
                logger.error(f"❌ IP CHECK FAILED: Navigation error")
                return {"ip": None, "verification_success": False, "error": "Navigation failed"}

            # Get page content
            content_result = await self.client.call_tool("browser_get_content", {"selector": "body"})

            if content_result.success and content_result.content:
                content = str(content_result.content)
                ip_match = re.search(r'"ip"\s*:\s*"([^"]+)"', content)

                if not ip_match:
                    # Try plain text format
                    ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', content)

                if ip_match:
                    ip_address = ip_match.group(1)
                    logger.info(f"🌐 OUTBOUND IP CHECK: {{'ip': '{ip_address}'}}")

                    # Heuristic for datacenter IPs
                    is_likely_datacenter = any(
                        ip_address.startswith(prefix) for prefix in
                        ["34.", "35.", "104.", "52.", "54.", "18.", "3.", "13.", "23.", "44."]
                    )

                    if is_likely_datacenter:
                        logger.warning(f"⚠️ IP {ip_address} LOOKS LIKE DATACENTER!")
                    else:
                        logger.info(f"✅ IP {ip_address} does NOT match datacenter ranges")

                    return {
                        "ip": ip_address,
                        "is_likely_datacenter": is_likely_datacenter,
                        "verification_success": True
                    }

            logger.error("❌ Could not extract IP from response")
            return {"ip": None, "verification_success": False, "error": "Parse error"}

        except Exception as e:
            logger.error(f"❌ IP VERIFICATION FAILED: {e}")
            return {"ip": None, "verification_success": False, "error": str(e)}
        finally:
            logger.info("=" * 70)

    async def _detect_page_state(self) -> str:
        """
        Detect the current page state after navigation.

        Returns one of:
        - "healthy": Normal Uber Eats page
        - "bot_challenge_detected": Captcha/challenge
        - "geo_selector_detected": Country selector blocking
        - "consent_blocking": Cookie/GDPR wall
        - "navigation_stalled": Page didn't load properly
        """
        logger.info("Detecting page state...")

        # Check for bot challenges first
        for selector in SELECTORS["bot_challenge"]:
            if await self._probe_selector(selector, timeout=2000):
                logger.warning(f"BOT CHALLENGE detected: {selector}")
                return "bot_challenge_detected"

        # Check for geo/country selector
        for selector in SELECTORS["geo_selector"]:
            if await self._probe_selector(selector, timeout=2000):
                logger.warning(f"GEO SELECTOR detected: {selector}")
                return "geo_selector_detected"

        # Check for consent wall (but these can often be dismissed)
        for selector in SELECTORS["consent_wall"]:
            if await self._probe_selector(selector, timeout=2000):
                logger.info(f"Consent wall detected: {selector} - will try to dismiss")
                # Attempt to dismiss - navigation handler already tries this
                break

        # Check for age gate
        for selector in SELECTORS["age_gate"]:
            if await self._probe_selector(selector, timeout=2000):
                logger.warning(f"AGE GATE detected: {selector}")
                return "age_gate_blocking"

        # Check if we can find ANY interactive element
        any_interactive = False
        for selector in SELECTORS["address_reveal_button"] + SELECTORS["address_input"]:
            if await self._probe_selector(selector, timeout=2000):
                any_interactive = True
                logger.info(f"Found interactive element: {selector}")
                break

        if not any_interactive:
            logger.warning("No interactive elements found - page may be stalled or degraded")
            return "navigation_stalled"

        return "healthy"

    # ========================================================================
    # Phase A: Setup
    # ========================================================================

    async def _setup_location(self, address: str) -> str:
        """
        Navigate to Uber Eats and set delivery location.

        Returns:
        - "success" if location was set
        - A failure reason string otherwise (must be valid FailureReason)
        """
        try:
            # ================================================================
            # STEP 0: MANDATORY IP VERIFICATION (before Uber Eats)
            # ================================================================
            logger.info("Step 0: Verifying outbound IP address...")

            # Call the runtime's IP verification method
            if hasattr(self.client, 'runtime') and hasattr(self.client.runtime, 'verify_outbound_ip'):
                ip_result = await self.client.runtime.verify_outbound_ip()
            else:
                # Direct call for MCP client that wraps runtime
                ip_result = await self._verify_outbound_ip()

            # Store IP info in debug
            self.debug.outbound_ip = ip_result.get('ip')
            self.debug.ip_is_datacenter = ip_result.get('is_likely_datacenter', True)

            if ip_result.get('is_likely_datacenter'):
                logger.warning("⚠️ OUTBOUND IP APPEARS TO BE DATACENTER - Uber Eats may block!")
            else:
                logger.info(f"✅ Outbound IP {ip_result.get('ip')} looks residential")

            # Step 1: Navigate to Uber Eats
            logger.info("Step 1: Navigating to Uber Eats...")
            nav_result = await self.client.navigate("https://www.ubereats.com")

            # Record navigation attempts in debug
            self.debug.nav_attempts = getattr(nav_result, 'attempts', 1)

            if not nav_result.success:
                logger.error(f"Navigation failed after {self.debug.nav_attempts} attempts: {nav_result.error}")
                # DON'T screenshot on failure - page is stalled
                return "uber_eats_unavailable"

            # Navigation succeeded - now safe to take screenshot
            logger.info("Navigation succeeded, waiting for page to stabilize...")
            await asyncio.sleep(2)

            # Step 2: Detect page state (this is our readiness check)
            logger.info("Step 2: Detecting page state...")
            page_state = await self._detect_page_state()

            # Only screenshot AFTER page state detection (page is responsive)
            if page_state == "healthy":
                await self._take_debug_screenshot("S2_PAGE_HEALTHY")
            else:
                # Page not healthy - try one screenshot but don't block on it
                logger.warning(f"Page state: {page_state} - attempting diagnostic screenshot")
                try:
                    await asyncio.wait_for(
                        self._take_debug_screenshot(f"S2_{page_state.upper()}"),
                        timeout=3.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("Screenshot timed out on unhealthy page")
                return page_state  # Return the detected state as failure reason

            # Step 3: Find and activate address input
            logger.info("Step 3: Activating address input...")
            address_input_found = await self._activate_address_input()

            if not address_input_found:
                await self._take_debug_screenshot("S3_ADDRESS_INPUT_NOT_FOUND")
                return "address_not_serviceable"

            await self._take_debug_screenshot("S3_ADDRESS_INPUT_ACTIVE")

            # Step 4: Type address
            logger.info(f"Step 4: Typing address: {address}")
            type_success = await self._type_address(address)

            if not type_success:
                await self._take_debug_screenshot("S4_TYPE_FAILED")
                return "address_not_serviceable"

            await asyncio.sleep(2.5)  # Wait for autocomplete
            await self._take_debug_screenshot("S4_AFTER_TYPING")

            # Step 5: Select address from suggestions (with keyboard fallback)
            logger.info("Step 5: Selecting address...")
            select_success = await self._select_address_suggestion()

            await self._take_debug_screenshot("S5_AFTER_SELECTION")

            if not select_success:
                logger.warning("Could not select address through normal flow")
                return "address_not_serviceable"

            # Step 6: Verify address was set
            logger.info("Step 6: Verifying address was set...")
            await asyncio.sleep(2)

            # Check if we can find search (indicates we're on the main feed)
            for selector in SELECTORS["search_input"]:
                if await self._probe_selector(selector, timeout=3000):
                    logger.info("✅ Address appears to be set - search input visible")
                    await self._take_debug_screenshot("S6_SUCCESS")
                    return "success"

            # Alternatively, check if address is displayed
            for selector in SELECTORS["address_display"]:
                if await self._probe_selector(selector, timeout=2000):
                    logger.info("✅ Address appears in header")
                    await self._take_debug_screenshot("S6_SUCCESS")
                    return "success"

            logger.warning("Could not verify address was set")
            await self._take_debug_screenshot("S6_VERIFICATION_FAILED")
            return "address_not_serviceable"

        except Exception as e:
            logger.exception(f"Setup location failed: {e}")
            await self._take_debug_screenshot("ERROR_EXCEPTION")
            return "unknown_error"

    async def _activate_address_input(self) -> bool:
        """Find and click the address input, or reveal button first if needed."""

        # First, try clicking address input directly
        for selector in SELECTORS["address_input"]:
            try:
                result = await self.client.click(selector)
                if result.success:
                    logger.info(f"Clicked address input: {selector}")
                    await asyncio.sleep(0.5)
                    return True
            except Exception:
                continue

        # If that failed, try clicking a reveal button first
        logger.info("Address input not directly clickable, trying reveal buttons...")
        for selector in SELECTORS["address_reveal_button"]:
            try:
                result = await self.client.click(selector)
                if result.success:
                    logger.info(f"Clicked reveal button: {selector}")
                    await asyncio.sleep(1)

                    # Now try address input again
                    for input_selector in SELECTORS["address_input"]:
                        try:
                            result = await self.client.click(input_selector)
                            if result.success:
                                logger.info(f"Clicked address input after reveal: {input_selector}")
                                await asyncio.sleep(0.5)
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        return False

    async def _type_address(self, address: str) -> bool:
        """Type the address into the active input field."""
        for selector in SELECTORS["address_input"]:
            try:
                result = await self.client.fill(selector, address)
                if result.success:
                    logger.info(f"Typed address into: {selector}")
                    return True
            except Exception:
                continue
        return False

    async def _select_address_suggestion(self) -> bool:
        """
        Select an address from autocomplete suggestions.

        Uses multiple strategies:
        1. Click on visible suggestion element
        2. Keyboard navigation (ArrowDown + Enter)
        3. Just press Enter to submit
        """

        # Strategy 1: Try clicking a suggestion
        logger.info("Trying to click suggestion...")
        for selector in SELECTORS["address_suggestion"]:
            try:
                if await self._probe_selector(selector, timeout=2000):
                    result = await self.client.click(selector)
                    if result.success:
                        logger.info(f"✅ Clicked suggestion: {selector}")
                        await asyncio.sleep(1)

                        # Check for confirm button
                        for confirm in SELECTORS["address_confirm"]:
                            try:
                                if await self._probe_selector(confirm, timeout=1500):
                                    await self.client.click(confirm)
                                    logger.info(f"Clicked confirm: {confirm}")
                            except Exception:
                                pass

                        return True
            except Exception:
                continue

        # Strategy 2: Keyboard navigation
        logger.info("No clickable suggestion found, trying keyboard navigation...")
        try:
            # Press ArrowDown to highlight first suggestion
            await self.client.call_tool("browser_press_key", {"key": "ArrowDown"})
            await asyncio.sleep(0.3)

            # Press Enter to select
            await self.client.call_tool("browser_press_key", {"key": "Enter"})
            await asyncio.sleep(1)

            logger.info("Sent ArrowDown + Enter")

            # Check if it worked by looking for confirm or search
            for confirm in SELECTORS["address_confirm"]:
                if await self._probe_selector(confirm, timeout=1500):
                    await self.client.click(confirm)
                    return True

            # If search appeared, we're good
            for search in SELECTORS["search_input"]:
                if await self._probe_selector(search, timeout=2000):
                    return True

        except Exception as e:
            logger.warning(f"Keyboard navigation failed: {e}")

        # Strategy 3: Just press Enter (maybe address is already valid)
        logger.info("Final fallback: pressing Enter to submit...")
        try:
            await self.client.call_tool("browser_press_key", {"key": "Enter"})
            await asyncio.sleep(2)

            # Check result
            for search in SELECTORS["search_input"]:
                if await self._probe_selector(search, timeout=2000):
                    return True
        except Exception:
            pass

        return False

    # ========================================================================
    # Phase B: Search
    # ========================================================================

    async def _search_restaurants(
        self,
        search_terms: List[str],
        max_restaurants: int
    ) -> List[str]:
        """Search for restaurants and collect unique URLs."""
        all_urls: set = set()

        for term in search_terms:
            logger.info(f"Searching for: '{term}'")
            urls = await self._execute_search(term)
            all_urls.update(urls)
            logger.info(f"Found {len(urls)} restaurants for '{term}', total unique: {len(all_urls)}")

            if len(all_urls) >= max_restaurants:
                break

            await asyncio.sleep(1.5)

        result = list(all_urls)[:max_restaurants]
        logger.info(f"Final: {len(result)} unique restaurant URLs")
        return result

    async def _execute_search(self, term: str) -> List[str]:
        """Execute a single search and extract restaurant URLs."""
        try:
            # Find search input
            search_input = None
            for selector in SELECTORS["search_input"]:
                if await self._probe_selector(selector, timeout=3000):
                    search_input = selector
                    break

            if not search_input:
                logger.warning("Could not find search input")
                return []

            # Click and fill search
            await self.client.click(search_input)
            await asyncio.sleep(0.3)
            await self.client.fill(search_input, "")
            await asyncio.sleep(0.2)
            await self.client.fill(search_input, term)
            await asyncio.sleep(0.5)

            # Submit search
            await self.client.call_tool("browser_press_key", {"key": "Enter"})
            await asyncio.sleep(3)

            # Scroll to load more
            await self._scroll_results()

            # Extract restaurant links
            return await self._extract_restaurant_links()

        except Exception as e:
            logger.exception(f"Search execution failed for '{term}': {e}")
            return []

    async def _scroll_results(self, max_scrolls: int = 5):
        """Scroll to load lazy content."""
        for i in range(max_scrolls):
            try:
                await self.client.call_tool("browser_scroll", {"direction": "down"})
                await asyncio.sleep(0.8)
            except Exception:
                break

    async def _extract_restaurant_links(self) -> List[str]:
        """Extract restaurant URLs from current page using extract tool."""
        urls = []

        for selector in SELECTORS["restaurant_card"]:
            try:
                result = await self.client.call_tool(
                    "browser_extract",
                    {"selector": selector, "extract_mode": "href", "attribute": "href"}
                )
                if result.success and result.extracted_data:
                    data = result.extracted_data
                    if isinstance(data, list):
                        for link in data:
                            if isinstance(link, str) and "/store/" in link:
                                if link.startswith("/"):
                                    link = f"https://www.ubereats.com{link}"
                                urls.append(link)
                    elif isinstance(data, str) and "/store/" in data:
                        if data.startswith("/"):
                            data = f"https://www.ubereats.com{data}"
                        urls.append(data)

                if urls:
                    break
            except Exception:
                continue

        return list(set(urls))

    # ========================================================================
    # Phase C: Extract
    # ========================================================================

    async def _extract_restaurants(
        self,
        restaurant_urls: List[str],
        max_items_per_restaurant: int
    ) -> List[ExtractedRestaurant]:
        """Extract menu data from each restaurant."""
        restaurants = []

        for url in restaurant_urls:
            logger.info(f"Extracting from: {url}")

            # Check for bot detection
            page_state = await self._detect_page_state()
            if page_state == "bot_challenge_detected":
                self.debug.blocked_at_step = f"restaurant_{len(restaurants)}"
                logger.warning("Bot detection triggered, stopping extraction")
                break

            restaurant = await self._extract_single_restaurant(url, max_items_per_restaurant)
            if restaurant:
                restaurants.append(restaurant)
                self.debug.restaurants_scanned += 1
            else:
                self.debug.restaurants_skipped += 1

            await asyncio.sleep(2)

        return restaurants

    async def _extract_single_restaurant(
        self,
        url: str,
        max_items: int
    ) -> Optional[ExtractedRestaurant]:
        """Extract menu items from a single restaurant."""
        try:
            nav_result = await self.client.navigate(url)
            if not nav_result.success:
                logger.error(f"Failed to navigate to {url}")
                return None

            await asyncio.sleep(2)

            # Extract restaurant name
            name = await self._extract_first_text(SELECTORS["restaurant_name"])
            if not name:
                name = "Unknown Restaurant"

            # Extract ETA
            eta_text = await self._extract_first_text(SELECTORS["restaurant_eta"])
            eta_minutes = self._parse_eta(eta_text)

            # Scroll to load menu
            await self._scroll_results(max_scrolls=8)

            # Extract menu items by clicking into them
            items = await self._extract_menu_items(max_items)

            # Process items (add protein estimation)
            processed_items = [process_extracted_item(item) for item in items]

            return ExtractedRestaurant(
                name=name,
                url=url,
                eta_minutes=eta_minutes,
                items=processed_items
            )

        except Exception as e:
            logger.exception(f"Restaurant extraction failed: {e}")
            return None

    async def _extract_first_text(self, selectors: List[str]) -> Optional[str]:
        """Extract text from first matching selector."""
        for selector in selectors:
            try:
                result = await self.client.call_tool(
                    "browser_extract",
                    {"selector": selector, "extract_mode": "text"}
                )
                if result.success and result.extracted_data:
                    text = result.extracted_data
                    if isinstance(text, list) and text:
                        text = text[0]
                    if isinstance(text, str) and text.strip():
                        return text.strip()
            except Exception:
                continue
        return None

    async def _extract_menu_items(self, max_items: int) -> List[ExtractedMenuItem]:
        """
        Extract menu items from current restaurant page.

        Since we can't use browser_evaluate, we use browser_extract to get
        text content and browser_click to open item modals for details.
        """
        items = []

        # First, get count of menu items
        item_count = 0
        for selector in SELECTORS["menu_item_card"]:
            try:
                result = await self.client.call_tool(
                    "browser_get_element_count",
                    {"selector": selector}
                )
                if result.success and result.content:
                    count = int(result.content) if isinstance(result.content, (int, str)) else 0
                    if count > 0:
                        item_count = min(count, max_items)
                        logger.info(f"Found {count} menu items with selector: {selector}")
                        break
            except Exception:
                continue

        if item_count == 0:
            logger.warning("No menu items found")
            return []

        # Extract items by getting text content of each
        for selector in SELECTORS["menu_item_card"]:
            try:
                # Extract all item names
                names_result = await self.client.call_tool(
                    "browser_extract",
                    {"selector": f"{selector} h3, {selector} [data-testid*='title']", "extract_mode": "text"}
                )

                # Extract all prices
                prices_result = await self.client.call_tool(
                    "browser_extract",
                    {"selector": f"{selector} [data-testid*='price'], {selector} span[class*='price']", "extract_mode": "text"}
                )

                # Extract all descriptions
                desc_result = await self.client.call_tool(
                    "browser_extract",
                    {"selector": f"{selector} p, {selector} [data-testid*='description']", "extract_mode": "text"}
                )

                # Extract all links
                links_result = await self.client.call_tool(
                    "browser_extract",
                    {"selector": f"{selector} a, {selector}", "extract_mode": "href", "attribute": "href"}
                )

                names = names_result.extracted_data if names_result.success else []
                prices = prices_result.extracted_data if prices_result.success else []
                descs = desc_result.extracted_data if desc_result.success else []
                links = links_result.extracted_data if links_result.success else []

                if not isinstance(names, list):
                    names = [names] if names else []
                if not isinstance(prices, list):
                    prices = [prices] if prices else []
                if not isinstance(descs, list):
                    descs = [descs] if descs else []
                if not isinstance(links, list):
                    links = [links] if links else []

                # Combine into items
                for i in range(min(len(names), max_items)):
                    name = names[i] if i < len(names) else None
                    price_text = prices[i] if i < len(prices) else None
                    desc = descs[i] if i < len(descs) else None
                    link = links[i] if i < len(links) else f"#item-{i}"

                    if not name:
                        continue

                    price = parse_price(price_text) if price_text else None
                    if price is None:
                        continue

                    items.append(ExtractedMenuItem(
                        item_name=str(name).strip(),
                        price=price,
                        protein_grams=None,  # Will be estimated
                        calories=None,
                        description=str(desc).strip()[:300] if desc else None,
                        url=link if link else f"#item-{i}"
                    ))

                if items:
                    break

            except Exception as e:
                logger.debug(f"Menu extraction with {selector} failed: {e}")
                continue

        logger.info(f"Extracted {len(items)} menu items")
        return items[:max_items]

    # ========================================================================
    # Utility Methods
    # ========================================================================

    def _parse_eta(self, eta_text: Optional[str]) -> Optional[int]:
        """Parse ETA text into minutes."""
        if not eta_text:
            return None

        import re
        match = re.search(r'(\d+)(?:\s*[-–]\s*(\d+))?\s*min', eta_text, re.IGNORECASE)
        if match:
            low = int(match.group(1))
            high = int(match.group(2)) if match.group(2) else low
            return (low + high) // 2

        match = re.search(r'(\d+)', eta_text)
        if match:
            return int(match.group(1))

        return None

    def _failure_output(
        self,
        input_config: FoodDeliveryInput,
        constraints: FoodDeliveryConstraints,
        reason: str
    ) -> FoodDeliveryOutput:
        """Create a failure output."""
        duration_ms = int((time.time() - self._start_time) * 1000)

        # Attach debug screenshots
        if self._debug_screenshots:
            self.debug.debug_screenshots = self._debug_screenshots

        return FoodDeliveryOutput.failure(
            location=input_config.delivery_address,
            constraints=constraints,
            search_terms=input_config.search_terms,
            reason=reason,
            debug=self.debug
        )


# ============================================================================
# Public API
# ============================================================================

async def run_food_delivery_workflow(
    mcp_client,
    input_config: FoodDeliveryInput
) -> FoodDeliveryOutput:
    """Run the food delivery workflow."""
    executor = FoodDeliveryExecutor(mcp_client, headless=input_config.headless)
    return await executor.execute(input_config)
