"""
Food Delivery Workflow Executor for Uber Eats High-Protein Meal Finder.

Orchestrates the full browser automation workflow:
1. Navigate to Uber Eats and set delivery location
2. Search for high-protein restaurants
3. Extract menu items from each restaurant
4. Process nutrition data and estimate missing values
5. Assemble valid carts meeting protein/price constraints
6. Rank and return top 3 cart options
"""

import asyncio
import logging
import time
from typing import List, Optional, Tuple
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
# Uber Eats Selectors (may need periodic updates)
# ============================================================================

# These are example selectors - actual selectors need to be discovered
# by inspecting Uber Eats website structure

SELECTORS = {
    # Location/Address
    "address_input": [
        "input[data-testid='location-typeahead-input']",
        "input[placeholder*='Enter delivery address']",
        "input[aria-label*='delivery address']",
        "#location-typeahead-input",
    ],
    "address_suggestion": [
        "[data-testid='location-typeahead-suggestion']",
        "[role='option']",
        ".autocomplete-item",
    ],
    "address_confirm": [
        "button[data-testid='location-typeahead-confirm']",
        "button:has-text('Done')",
        "button:has-text('Save')",
    ],

    # Search
    "search_input": [
        "input[data-testid='search-input']",
        "input[placeholder*='Search']",
        "input[aria-label*='Search']",
    ],
    "search_submit": [
        "button[data-testid='search-submit']",
        "button[type='submit']",
    ],

    # Restaurant results
    "restaurant_card": [
        "[data-testid='store-card']",
        "a[href*='/store/']",
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

    # Menu items
    "menu_item_card": [
        "[data-testid='menu-item']",
        "[data-testid='store-item']",
        ".menu-item",
    ],
    "menu_item_name": [
        "[data-testid='item-title']",
        "h3",
        ".item-name",
    ],
    "menu_item_price": [
        "[data-testid='item-price']",
        "span[class*='price']",
        ".item-price",
    ],
    "menu_item_description": [
        "[data-testid='item-description']",
        "p",
        ".item-description",
    ],

    # Nutrition (if available)
    "nutrition_section": [
        "[data-testid='nutrition-info']",
        ".nutrition-facts",
        "[aria-label*='nutrition']",
    ],
    "nutrition_protein": [
        "[data-testid='protein']",
        "span:has-text('Protein')",
        ".protein-value",
    ],
    "nutrition_calories": [
        "[data-testid='calories']",
        "span:has-text('Cal')",
        ".calories-value",
    ],

    # Item modal
    "item_modal": [
        "[data-testid='item-modal']",
        "[role='dialog']",
        ".modal-content",
    ],
    "modal_close": [
        "[data-testid='modal-close']",
        "button[aria-label='Close']",
        ".close-button",
    ],

    # Bot detection
    "captcha": [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        ".cf-browser-verification",
    ],
}


class FoodDeliveryExecutor:
    """
    Executes the food delivery workflow using browser automation.
    """

    def __init__(self, mcp_client, headless: bool = True):
        """
        Initialize the executor.

        Args:
            mcp_client: MCP client for browser automation
            headless: Whether to run browser in headless mode
        """
        self.client = mcp_client
        self.headless = headless
        self.debug = DebugInfo()
        self._workflow_context: dict = {}
        self._start_time: float = 0

    async def execute(self, input_config: FoodDeliveryInput) -> FoodDeliveryOutput:
        """
        Execute the full food delivery workflow.

        Args:
            input_config: Workflow input configuration

        Returns:
            FoodDeliveryOutput with results or failure information
        """
        self._start_time = time.time()
        constraints = FoodDeliveryConstraints(
            min_protein_grams=input_config.min_protein_grams,
            max_price_usd=input_config.max_price_usd
        )

        try:
            # Phase A: Setup - Navigate and set location
            logger.info("Phase A: Setting up location...")
            success = await self._setup_location(input_config.delivery_address)
            if not success:
                return self._failure_output(
                    input_config, constraints, "address_not_serviceable"
                )

            # Phase B: Search - Find restaurants
            logger.info("Phase B: Searching for restaurants...")
            restaurant_urls = await self._search_restaurants(
                input_config.search_terms,
                input_config.max_restaurants
            )
            if not restaurant_urls:
                return self._failure_output(
                    input_config, constraints, "no_restaurants_found_for_search_terms"
                )

            # Phase C: Extract - Scan menus
            logger.info(f"Phase C: Extracting from {len(restaurant_urls)} restaurants...")
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
            logger.info("Phase D: Processing and ranking carts...")
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
                # Find best attempt for debug info
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
            return self._failure_output(
                input_config, constraints, "unknown_error"
            )

    # ========================================================================
    # Phase A: Setup
    # ========================================================================

    async def _setup_location(self, address: str) -> bool:
        """Navigate to Uber Eats and set delivery location."""
        try:
            # Navigate to Uber Eats
            nav_result = await self.client.navigate("https://www.ubereats.com")
            if not nav_result.success:
                logger.error(f"Navigation failed: {nav_result.error}")
                return False

            await asyncio.sleep(2)  # Wait for page to stabilize

            # Try to find and click address input
            address_input = await self._try_selectors(
                SELECTORS["address_input"], action="click"
            )
            if not address_input:
                logger.warning("Could not find address input")
                # Address might already be set, continue

            # Type address
            if address_input:
                await asyncio.sleep(0.5)
                type_result = await self.client.fill(address_input, address)
                if not type_result.success:
                    logger.error(f"Failed to type address: {type_result.error}")
                    return False

                await asyncio.sleep(1.5)  # Wait for autocomplete

                # Select first suggestion
                suggestion = await self._try_selectors(
                    SELECTORS["address_suggestion"], action="click"
                )
                if not suggestion:
                    logger.warning("No address suggestions found")
                    return False

                await asyncio.sleep(1)

            return True

        except Exception as e:
            logger.exception(f"Setup location failed: {e}")
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
            logger.info(f"Searching for: {term}")
            urls = await self._execute_search(term)
            all_urls.update(urls)

            if len(all_urls) >= max_restaurants:
                break

            await asyncio.sleep(1)  # Delay between searches

        # Cap at max_restaurants
        result = list(all_urls)[:max_restaurants]
        logger.info(f"Found {len(result)} unique restaurant URLs")
        return result

    async def _execute_search(self, term: str) -> List[str]:
        """Execute a single search and extract restaurant URLs."""
        try:
            # Find and use search input
            search_input = await self._try_selectors(
                SELECTORS["search_input"], action="click"
            )
            if not search_input:
                logger.warning("Could not find search input")
                return []

            # Clear and type search term
            await self.client.fill(search_input, "")
            await asyncio.sleep(0.3)
            await self.client.fill(search_input, term)
            await asyncio.sleep(0.5)

            # Submit search (press Enter or click submit)
            await self.client.call_tool("browser_press_key", {"key": "Enter"})
            await asyncio.sleep(2)  # Wait for results

            # Scroll to load more results
            await self._scroll_results()

            # Extract restaurant links
            links = await self._extract_restaurant_links()
            return links

        except Exception as e:
            logger.exception(f"Search execution failed for '{term}': {e}")
            return []

    async def _scroll_results(self, max_scrolls: int = 5):
        """Scroll search results to load lazy content."""
        for i in range(max_scrolls):
            await self.client.call_tool("browser_scroll", {"direction": "down"})
            await asyncio.sleep(0.8)

    async def _extract_restaurant_links(self) -> List[str]:
        """Extract restaurant URLs from search results page."""
        urls = []

        for selector in SELECTORS["restaurant_card"]:
            try:
                result = await self.client.call_tool(
                    "browser_extract_links",
                    {"selector": selector}
                )
                if result.success and result.extracted_data:
                    for link in result.extracted_data:
                        # Filter for Uber Eats store URLs
                        if isinstance(link, str) and "/store/" in link:
                            if link.startswith("/"):
                                link = f"https://www.ubereats.com{link}"
                            urls.append(link)
                    if urls:
                        break
            except Exception:
                continue

        return list(set(urls))  # Dedupe

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

            # Check for bot detection before each restaurant
            if await self._detect_bot_block():
                self.debug.blocked_at_step = f"restaurant_{len(restaurants)}"
                logger.warning("Bot detection triggered, stopping extraction")
                break

            restaurant = await self._extract_single_restaurant(
                url, max_items_per_restaurant
            )
            if restaurant:
                restaurants.append(restaurant)
                self.debug.restaurants_scanned += 1
            else:
                self.debug.restaurants_skipped += 1

            await asyncio.sleep(2)  # Delay between restaurants

        return restaurants

    async def _extract_single_restaurant(
        self,
        url: str,
        max_items: int
    ) -> Optional[ExtractedRestaurant]:
        """Extract menu items from a single restaurant."""
        try:
            # Navigate to restaurant page
            nav_result = await self.client.navigate(url)
            if not nav_result.success:
                logger.error(f"Failed to navigate to {url}")
                return None

            await asyncio.sleep(2)  # Wait for page load

            # Extract restaurant name
            name = await self._extract_text(SELECTORS["restaurant_name"])
            if not name:
                name = "Unknown Restaurant"

            # Extract ETA
            eta_text = await self._extract_text(SELECTORS["restaurant_eta"])
            eta_minutes = self._parse_eta(eta_text)

            # Scroll to load full menu
            await self._scroll_results(max_scrolls=8)

            # Extract menu items
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
            return ExtractedRestaurant(
                name="Unknown",
                url=url,
                items=[],
                extraction_success=False,
                extraction_error=str(e)
            )

    async def _extract_menu_items(self, max_items: int) -> List[ExtractedMenuItem]:
        """Extract menu items from the current restaurant page."""
        items = []

        # Get all menu item elements
        for selector in SELECTORS["menu_item_card"]:
            try:
                # First extract item names and prices from the main page
                item_data = await self._extract_items_from_page(selector, max_items)
                if item_data:
                    items.extend(item_data)
                    break
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue

        return items[:max_items]

    async def _extract_items_from_page(
        self,
        item_selector: str,
        max_items: int
    ) -> List[ExtractedMenuItem]:
        """Extract item data from menu cards on the page."""
        items = []

        # Get item count
        count_result = await self.client.call_tool(
            "browser_evaluate",
            {"script": f"document.querySelectorAll('{item_selector}').length"}
        )

        if not count_result.success:
            return []

        item_count = min(int(count_result.content or 0), max_items)

        for i in range(item_count):
            try:
                # Extract name
                name_script = f"""
                    (() => {{
                        const items = document.querySelectorAll('{item_selector}');
                        if (items[{i}]) {{
                            const nameEl = items[{i}].querySelector('h3, [data-testid="item-title"], .item-name');
                            return nameEl ? nameEl.textContent.trim() : null;
                        }}
                        return null;
                    }})()
                """
                name_result = await self.client.call_tool(
                    "browser_evaluate", {"script": name_script}
                )
                name = name_result.content if name_result.success else None

                if not name:
                    continue

                # Extract price
                price_script = f"""
                    (() => {{
                        const items = document.querySelectorAll('{item_selector}');
                        if (items[{i}]) {{
                            const priceEl = items[{i}].querySelector('[data-testid="item-price"], .item-price, span[class*="price"]');
                            return priceEl ? priceEl.textContent.trim() : null;
                        }}
                        return null;
                    }})()
                """
                price_result = await self.client.call_tool(
                    "browser_evaluate", {"script": price_script}
                )
                price_text = price_result.content if price_result.success else None
                price = parse_price(price_text) if price_text else None

                if price is None:
                    continue

                # Extract description (for protein estimation)
                desc_script = f"""
                    (() => {{
                        const items = document.querySelectorAll('{item_selector}');
                        if (items[{i}]) {{
                            const descEl = items[{i}].querySelector('p, [data-testid="item-description"], .item-description');
                            return descEl ? descEl.textContent.trim().substring(0, 300) : null;
                        }}
                        return null;
                    }})()
                """
                desc_result = await self.client.call_tool(
                    "browser_evaluate", {"script": desc_script}
                )
                description = desc_result.content if desc_result.success else None

                # Get item URL (current page + item identifier)
                url_script = f"""
                    (() => {{
                        const items = document.querySelectorAll('{item_selector}');
                        if (items[{i}]) {{
                            const link = items[{i}].closest('a') || items[{i}].querySelector('a');
                            return link ? link.href : window.location.href + '#item-{i}';
                        }}
                        return window.location.href + '#item-{i}';
                    }})()
                """
                url_result = await self.client.call_tool(
                    "browser_evaluate", {"script": url_script}
                )
                item_url = url_result.content if url_result.success else f"#item-{i}"

                # Try to get nutrition data if available
                protein, calories = await self._try_extract_nutrition(item_selector, i)

                items.append(ExtractedMenuItem(
                    item_name=name,
                    price=price,
                    protein_grams=protein,
                    calories=calories,
                    description=description,
                    url=item_url
                ))

            except Exception as e:
                logger.debug(f"Failed to extract item {i}: {e}")
                continue

        return items

    async def _try_extract_nutrition(
        self,
        item_selector: str,
        index: int
    ) -> Tuple[Optional[int], Optional[int]]:
        """Try to extract nutrition info from menu item."""
        # This depends heavily on Uber Eats showing nutrition data
        # Many restaurants don't have it visible
        protein = None
        calories = None

        try:
            nutrition_script = f"""
                (() => {{
                    const items = document.querySelectorAll('{item_selector}');
                    if (items[{index}]) {{
                        const text = items[{index}].textContent;
                        const proteinMatch = text.match(/(\\d+)\\s*g?\\s*protein/i);
                        const calMatch = text.match(/(\\d+)\\s*(?:cal|calories)/i);
                        return {{
                            protein: proteinMatch ? parseInt(proteinMatch[1]) : null,
                            calories: calMatch ? parseInt(calMatch[1]) : null
                        }};
                    }}
                    return {{protein: null, calories: null}};
                }})()
            """
            result = await self.client.call_tool(
                "browser_evaluate", {"script": nutrition_script}
            )
            if result.success and result.content:
                data = result.content
                protein = data.get("protein")
                calories = data.get("calories")
        except Exception:
            pass

        return protein, calories

    # ========================================================================
    # Utility Methods
    # ========================================================================

    async def _try_selectors(
        self,
        selectors: List[str],
        action: str = "click",
        value: str = None
    ) -> Optional[str]:
        """Try multiple selectors until one works."""
        for selector in selectors:
            try:
                if action == "click":
                    result = await self.client.click(selector)
                elif action == "fill":
                    result = await self.client.fill(selector, value or "")
                else:
                    result = await self.client.call_tool(
                        f"browser_{action}", {"selector": selector}
                    )

                if result.success:
                    return selector
            except Exception:
                continue
        return None

    async def _extract_text(self, selectors: List[str]) -> Optional[str]:
        """Extract text from first matching selector."""
        for selector in selectors:
            try:
                result = await self.client.call_tool(
                    "browser_extract_text",
                    {"selector": selector, "max_length": 200}
                )
                if result.success and result.extracted_data:
                    text = result.extracted_data
                    if isinstance(text, list):
                        text = text[0] if text else None
                    if text:
                        return text.strip()
            except Exception:
                continue
        return None

    async def _detect_bot_block(self) -> bool:
        """Check if bot detection has been triggered."""
        for selector in SELECTORS["captcha"]:
            try:
                result = await self.client.call_tool(
                    "browser_evaluate",
                    {"script": f"document.querySelector('{selector}') !== null"}
                )
                if result.success and result.content:
                    return True
            except Exception:
                continue
        return False

    def _parse_eta(self, eta_text: Optional[str]) -> Optional[int]:
        """Parse ETA text into minutes."""
        if not eta_text:
            return None

        import re
        # Look for patterns like "25-35 min", "30 min", "25–35"
        match = re.search(r'(\d+)(?:\s*[-–]\s*(\d+))?\s*min', eta_text, re.IGNORECASE)
        if match:
            low = int(match.group(1))
            high = int(match.group(2)) if match.group(2) else low
            return (low + high) // 2  # Average

        # Just a number
        match = re.search(r'(\d+)', eta_text)
        if match:
            return int(match.group(1))

        return None

    def _failure_output(
        self,
        input_config: FoodDeliveryInput,
        constraints: FoodDeliveryConstraints,
        reason: FailureReason
    ) -> FoodDeliveryOutput:
        """Create a failure output."""
        duration_ms = int((time.time() - self._start_time) * 1000)

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
    """
    Run the food delivery workflow.

    Args:
        mcp_client: MCP client for browser automation
        input_config: Workflow input configuration

    Returns:
        FoodDeliveryOutput with results
    """
    executor = FoodDeliveryExecutor(mcp_client, headless=input_config.headless)
    return await executor.execute(input_config)
