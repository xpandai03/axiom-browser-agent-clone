"""
Helper functions for the Uber Eats High-Protein Meal Finder agent.

Contains:
- Protein estimation from item names/descriptions
- Cart assembly algorithms
- Scoring and ranking logic
- Price parsing utilities
"""

import re
import logging
from typing import List, Optional, Tuple
from itertools import combinations

from shared.schemas.food_delivery import (
    ExtractedMenuItem,
    ExtractedRestaurant,
    CartItem,
    CartCandidate,
    RankedCartResult,
    FoodDeliveryConstraints,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Protein Estimation
# ============================================================================

# Keyword patterns and their estimated protein content (grams)
PROTEIN_KEYWORDS: List[Tuple[str, int, str]] = [
    # (pattern, estimated_grams, confidence)
    # High confidence estimates
    (r"grilled\s+chicken|chicken\s+breast", 35, "medium"),
    (r"double\s+chicken", 50, "medium"),
    (r"steak|ribeye|sirloin|filet", 30, "medium"),
    (r"salmon|tuna", 25, "medium"),
    (r"ground\s+beef|beef\s+patty", 25, "medium"),

    # Medium confidence estimates
    (r"chicken", 25, "low"),
    (r"beef|burger", 22, "low"),
    (r"fish|tilapia|cod|halibut", 22, "low"),
    (r"shrimp|prawns", 20, "low"),
    (r"pork|carnitas", 22, "low"),
    (r"lamb", 25, "low"),
    (r"turkey", 25, "low"),

    # Lower protein sources
    (r"tofu", 15, "medium"),
    (r"tempeh", 18, "medium"),
    (r"eggs?|egg\s+white", 6, "medium"),
    (r"black\s+beans|kidney\s+beans", 8, "low"),
    (r"lentils", 9, "low"),
    (r"chickpeas|hummus", 7, "low"),
    (r"quinoa", 8, "low"),
    (r"edamame", 11, "medium"),
    (r"greek\s+yogurt", 15, "medium"),
    (r"cottage\s+cheese", 12, "medium"),

    # Generic bowl indicators
    (r"protein\s+bowl", 30, "low"),
    (r"power\s+bowl", 28, "low"),
    (r"fitness\s+bowl", 28, "low"),
]

# Modifiers that add to base protein
PROTEIN_MODIFIERS: List[Tuple[str, int]] = [
    (r"double|2x", 15),
    (r"triple|3x", 30),
    (r"extra\s+(?:chicken|protein|meat)", 12),
    (r"large|grande", 5),
    (r"add\s+(?:chicken|protein)", 12),
    (r"with\s+(?:extra\s+)?(?:chicken|steak|beef)", 15),
]

# Maximum single-item protein estimate (sanity check)
MAX_SINGLE_ITEM_PROTEIN = 60


def estimate_protein_from_name(
    item_name: str,
    description: Optional[str] = None
) -> Tuple[Optional[int], str]:
    """
    Estimate protein content from item name and description.

    Args:
        item_name: Menu item name
        description: Optional item description

    Returns:
        Tuple of (estimated_grams or None, confidence level)
    """
    # Combine name and description for analysis
    text = f"{item_name} {description or ''}".lower()

    base_protein = 0
    matched_keywords = []

    # Find matching protein keywords
    for pattern, grams, confidence in PROTEIN_KEYWORDS:
        if re.search(pattern, text, re.IGNORECASE):
            # Take highest base protein if multiple matches
            if grams > base_protein:
                base_protein = grams
            matched_keywords.append((pattern, grams))

    if base_protein == 0:
        # No protein keywords found
        return None, "none"

    # Apply modifiers
    modifier_bonus = 0
    for pattern, bonus in PROTEIN_MODIFIERS:
        if re.search(pattern, text, re.IGNORECASE):
            modifier_bonus += bonus

    total_estimate = min(base_protein + modifier_bonus, MAX_SINGLE_ITEM_PROTEIN)

    # Determine confidence based on specificity
    if len(matched_keywords) > 1 or modifier_bonus > 0:
        confidence = "low"  # More assumptions = lower confidence
    elif any(conf == "medium" for _, _, conf in PROTEIN_KEYWORDS
             if re.search(_, text, re.IGNORECASE) for _ in [_]):
        confidence = "medium"
    else:
        confidence = "low"

    logger.debug(
        f"Estimated {total_estimate}g protein for '{item_name}' "
        f"(base: {base_protein}, modifier: {modifier_bonus}, confidence: {confidence})"
    )

    return total_estimate, confidence


def process_extracted_item(item: ExtractedMenuItem) -> ExtractedMenuItem:
    """
    Process an extracted menu item, adding protein estimation if needed.

    Args:
        item: Raw extracted menu item

    Returns:
        Processed item with protein_source and protein_eligible set
    """
    if item.protein_grams is not None:
        # Actual protein data available
        item.protein_source = "actual"
        item.protein_eligible = True
    else:
        # Try to estimate from name/description
        estimated, confidence = estimate_protein_from_name(
            item.item_name,
            item.description
        )

        if estimated is not None:
            item.protein_grams = estimated
            item.protein_source = "estimated"
            item.protein_eligible = True
            logger.info(
                f"Estimated {estimated}g protein for '{item.item_name}' "
                f"(confidence: {confidence})"
            )
        else:
            item.protein_source = None
            item.protein_eligible = False
            logger.debug(f"Could not estimate protein for '{item.item_name}'")

    return item


# ============================================================================
# Price Parsing
# ============================================================================

def parse_price(price_text: str) -> Optional[float]:
    """
    Parse a price string into a float.

    Args:
        price_text: Price string (e.g., "$12.99", "12.99 USD", "12,99")

    Returns:
        Price as float, or None if parsing fails
    """
    if not price_text:
        return None

    # Remove currency symbols and whitespace
    cleaned = re.sub(r'[^\d.,]', '', price_text.strip())

    # Handle European format (12,99 -> 12.99)
    if ',' in cleaned and '.' not in cleaned:
        cleaned = cleaned.replace(',', '.')
    elif ',' in cleaned and '.' in cleaned:
        # Remove thousand separators (1,234.99 -> 1234.99)
        cleaned = cleaned.replace(',', '')

    try:
        return float(cleaned)
    except ValueError:
        logger.warning(f"Could not parse price: '{price_text}'")
        return None


def parse_nutrition_text(text: str, nutrient: str = "protein") -> Optional[int]:
    """
    Parse nutrition value from text (e.g., "45g protein", "Protein: 45g").

    Args:
        text: Nutrition text
        nutrient: Nutrient to extract ("protein", "calories")

    Returns:
        Nutrient value as int, or None if not found
    """
    if not text:
        return None

    patterns = [
        rf"(\d+)\s*g?\s*{nutrient}",  # "45g protein" or "45 protein"
        rf"{nutrient}[:\s]+(\d+)\s*g?",  # "protein: 45g" or "Protein 45"
        rf"(\d+)\s*grams?\s*(?:of\s+)?{nutrient}",  # "45 grams of protein"
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue

    return None


# ============================================================================
# Cart Assembly
# ============================================================================

def find_valid_carts(
    restaurant: ExtractedRestaurant,
    min_protein: int,
    max_price: float,
    max_items_per_cart: int = 5
) -> List[CartCandidate]:
    """
    Find all valid cart combinations from a restaurant's menu.

    Args:
        restaurant: Restaurant with extracted items
        min_protein: Minimum protein target (grams)
        max_price: Maximum cart price (USD)
        max_items_per_cart: Maximum items allowed in a cart

    Returns:
        List of valid cart candidates
    """
    eligible_items = restaurant.eligible_items

    if not eligible_items:
        logger.debug(f"No eligible items for {restaurant.name}")
        return []

    valid_carts: List[CartCandidate] = []

    # Sort by protein per dollar (best value first)
    sorted_items = sorted(
        eligible_items,
        key=lambda x: x.protein_per_dollar,
        reverse=True
    )

    # Try single-item carts
    for item in sorted_items:
        if item.protein_grams >= min_protein and item.price < max_price:
            cart = _create_cart_candidate(
                restaurant=restaurant,
                items=[item]
            )
            valid_carts.append(cart)
            logger.debug(f"Found single-item cart: {item.item_name}")

    # Try 2-item combinations
    for combo in combinations(sorted_items, 2):
        cart = _try_combination(restaurant, list(combo), min_protein, max_price)
        if cart:
            valid_carts.append(cart)

    # Try 3-item combinations
    for combo in combinations(sorted_items, 3):
        cart = _try_combination(restaurant, list(combo), min_protein, max_price)
        if cart:
            valid_carts.append(cart)

    # Try greedy assembly for larger combinations
    greedy_cart = _greedy_cart_assembly(
        restaurant, sorted_items, min_protein, max_price, max_items_per_cart
    )
    if greedy_cart and greedy_cart not in valid_carts:
        valid_carts.append(greedy_cart)

    logger.info(f"Found {len(valid_carts)} valid carts for {restaurant.name}")
    return valid_carts


def _try_combination(
    restaurant: ExtractedRestaurant,
    items: List[ExtractedMenuItem],
    min_protein: int,
    max_price: float
) -> Optional[CartCandidate]:
    """Try a specific item combination and return cart if valid."""
    total_price = sum(item.price for item in items)
    total_protein = sum(item.protein_grams or 0 for item in items)

    if total_protein >= min_protein and total_price < max_price:
        return _create_cart_candidate(restaurant, items)

    return None


def _greedy_cart_assembly(
    restaurant: ExtractedRestaurant,
    sorted_items: List[ExtractedMenuItem],
    min_protein: int,
    max_price: float,
    max_items: int
) -> Optional[CartCandidate]:
    """
    Greedy algorithm to build a cart by adding best-value items.
    """
    cart_items: List[ExtractedMenuItem] = []
    current_protein = 0
    current_price = 0.0

    for item in sorted_items:
        if len(cart_items) >= max_items:
            break

        if current_price + item.price < max_price:
            cart_items.append(item)
            current_protein += item.protein_grams or 0
            current_price += item.price

            if current_protein >= min_protein:
                return _create_cart_candidate(restaurant, cart_items)

    return None


def _create_cart_candidate(
    restaurant: ExtractedRestaurant,
    items: List[ExtractedMenuItem]
) -> CartCandidate:
    """Create a CartCandidate from a list of items."""
    cart_items = [
        CartItem(
            item_name=item.item_name,
            price=item.price,
            protein_grams=item.protein_grams or 0,
            protein_source=item.protein_source,
            calories=item.calories,
            url=item.url
        )
        for item in items
    ]

    includes_estimates = any(
        item.protein_source == "estimated" for item in cart_items
    )

    return CartCandidate(
        restaurant=restaurant.name,
        restaurant_url=restaurant.url,
        cart_items=cart_items,
        total_price=sum(item.price for item in cart_items),
        total_protein_grams=sum(item.protein_grams for item in cart_items),
        includes_estimates=includes_estimates,
        eta_minutes=restaurant.eta_minutes
    )


# ============================================================================
# Scoring and Ranking
# ============================================================================

def score_cart(
    cart: CartCandidate,
    min_protein: int,
    max_price: float
) -> float:
    """
    Calculate a ranking score for a cart candidate.

    Scoring formula:
    - 40% weight: Protein achievement (how much over minimum)
    - 30% weight: Price efficiency (how much under budget)
    - 20% weight: Protein per dollar value
    - 10% weight: Delivery speed

    Penalties:
    - 15% penalty if cart includes estimated protein
    - Small penalty for carts with more than 2 items

    Args:
        cart: Cart candidate to score
        min_protein: Minimum protein target
        max_price: Maximum price budget

    Returns:
        Score (higher is better)
    """
    # Base score components
    protein_achievement = (cart.total_protein_grams / min_protein) * 40
    price_efficiency = (1 - cart.total_price / max_price) * 30
    value_ratio = min(cart.protein_per_dollar / 5, 1.0) * 20  # Cap at 5g/$

    # Delivery speed (normalize ETA, faster = higher score)
    if cart.eta_minutes and cart.eta_minutes > 0:
        speed_score = (1 / cart.eta_minutes) * 100 * 10
        speed_score = min(speed_score, 10)  # Cap at 10 points
    else:
        speed_score = 5  # Default middle score if ETA unknown

    base_score = protein_achievement + price_efficiency + value_ratio + speed_score

    # Apply penalties
    if cart.includes_estimates:
        base_score *= 0.85  # 15% penalty for estimated data

    if cart.item_count > 2:
        base_score -= (cart.item_count - 2) * 2  # Complexity penalty

    return round(base_score, 2)


def rank_carts(
    carts: List[CartCandidate],
    min_protein: int,
    max_price: float,
    top_n: int = 3
) -> List[RankedCartResult]:
    """
    Score and rank cart candidates, returning the top N.

    Args:
        carts: List of cart candidates
        min_protein: Minimum protein target
        max_price: Maximum price budget
        top_n: Number of results to return

    Returns:
        Top N ranked cart results
    """
    if not carts:
        return []

    # Score all carts
    for cart in carts:
        cart.score = score_cart(cart, min_protein, max_price)

    # Sort by score (descending), then apply tie-breakers
    sorted_carts = sorted(
        carts,
        key=lambda c: (
            c.score,
            c.actual_protein_ratio,  # Prefer actual data
            c.total_protein_grams,   # Higher protein
            -c.total_price,          # Lower price
            -(c.eta_minutes or 999), # Faster delivery
            -c.item_count            # Fewer items
        ),
        reverse=True
    )

    # Deduplicate by restaurant (keep only best cart per restaurant)
    seen_restaurants = set()
    unique_carts = []
    for cart in sorted_carts:
        if cart.restaurant not in seen_restaurants:
            seen_restaurants.add(cart.restaurant)
            unique_carts.append(cart)

    # Take top N
    top_carts = unique_carts[:top_n]

    # Create ranked results with reasons
    results = []
    for i, cart in enumerate(top_carts, start=1):
        reason = _generate_cart_reason(cart, i, min_protein, max_price)

        results.append(RankedCartResult(
            rank=i,
            restaurant=cart.restaurant,
            restaurant_url=cart.restaurant_url,
            cart_items=cart.cart_items,
            total_price=cart.total_price,
            total_protein_grams=cart.total_protein_grams,
            includes_estimates=cart.includes_estimates,
            eta_minutes=cart.eta_minutes,
            score=cart.score,
            reason=reason
        ))

    return results


def _generate_cart_reason(
    cart: CartCandidate,
    rank: int,
    min_protein: int,
    max_price: float
) -> str:
    """Generate a human-readable reason for why this cart was selected."""
    reasons = []

    # Protein achievement
    protein_surplus = cart.total_protein_grams - min_protein
    if protein_surplus > 20:
        reasons.append(f"{cart.total_protein_grams}g protein (+{protein_surplus}g over target)")
    else:
        reasons.append(f"{cart.total_protein_grams}g protein")

    # Price efficiency
    price_savings = max_price - cart.total_price
    if price_savings > 5:
        reasons.append(f"${cart.total_price:.2f} (${price_savings:.2f} under budget)")
    else:
        reasons.append(f"${cart.total_price:.2f}")

    # Value highlight
    if cart.protein_per_dollar > 4:
        reasons.append(f"excellent value ({cart.protein_per_dollar:.1f}g/$)")

    # Delivery speed
    if cart.eta_minutes and cart.eta_minutes < 30:
        reasons.append(f"fast delivery ({cart.eta_minutes} min)")

    # Estimate warning
    if cart.includes_estimates:
        reasons.append("*some nutrition estimated from menu names")

    if rank == 1:
        return "Best overall: " + ", ".join(reasons)
    elif rank == 2:
        return "Runner-up: " + ", ".join(reasons)
    else:
        return "Alternative: " + ", ".join(reasons)


# ============================================================================
# Aggregation Utilities
# ============================================================================

def aggregate_all_carts(
    restaurants: List[ExtractedRestaurant],
    constraints: FoodDeliveryConstraints
) -> Tuple[List[CartCandidate], int, int]:
    """
    Find all valid carts across all restaurants.

    Args:
        restaurants: List of restaurants with extracted items
        constraints: Protein and price constraints

    Returns:
        Tuple of (all valid carts, total items extracted, items with protein)
    """
    all_carts: List[CartCandidate] = []
    total_items = 0
    items_with_protein = 0

    for restaurant in restaurants:
        total_items += len(restaurant.items)
        items_with_protein += len([
            item for item in restaurant.items
            if item.protein_grams is not None
        ])

        carts = find_valid_carts(
            restaurant,
            constraints.min_protein_grams,
            constraints.max_price_usd
        )
        all_carts.extend(carts)

    logger.info(
        f"Found {len(all_carts)} valid carts from {len(restaurants)} restaurants "
        f"({total_items} items, {items_with_protein} with protein data)"
    )

    return all_carts, total_items, items_with_protein


def find_best_attempt(
    restaurants: List[ExtractedRestaurant],
    min_protein: int,
    max_price: float
) -> Optional[dict]:
    """
    Find the best cart attempt even if it doesn't meet constraints.
    Useful for debug output when no valid carts exist.
    """
    best_protein = 0
    best_attempt = None

    for restaurant in restaurants:
        eligible = restaurant.eligible_items
        if not eligible:
            continue

        # Sort by protein
        sorted_items = sorted(
            eligible,
            key=lambda x: x.protein_grams or 0,
            reverse=True
        )

        # Try to build best possible cart under budget
        cart_items = []
        total_protein = 0
        total_price = 0.0

        for item in sorted_items:
            if total_price + item.price < max_price:
                cart_items.append(item)
                total_protein += item.protein_grams or 0
                total_price += item.price

        if total_protein > best_protein:
            best_protein = total_protein
            shortfall = min_protein - total_protein
            best_attempt = {
                "restaurant": restaurant.name,
                "protein": total_protein,
                "price": round(total_price, 2),
                "shortfall": f"{shortfall}g protein" if shortfall > 0 else None,
                "items": len(cart_items)
            }

    return best_attempt
