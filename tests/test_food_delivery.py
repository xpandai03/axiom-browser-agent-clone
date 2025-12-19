"""
Unit tests for the Uber Eats High-Protein Meal Finder workflow.

Tests cover:
- Protein estimation from item names
- Price parsing
- Cart assembly logic
- Scoring and ranking
- Input/output schema validation
"""

import pytest
from typing import List

# Import schemas
from shared.schemas.food_delivery import (
    FoodDeliveryInput,
    FoodDeliveryOutput,
    FoodDeliveryConstraints,
    ExtractedMenuItem,
    ExtractedRestaurant,
    CartItem,
    CartCandidate,
    RankedCartResult,
)

# Import helper functions
from services.api.food_delivery_helpers import (
    estimate_protein_from_name,
    process_extracted_item,
    parse_price,
    parse_nutrition_text,
    find_valid_carts,
    score_cart,
    rank_carts,
    aggregate_all_carts,
    find_best_attempt,
)


# ============================================================================
# Protein Estimation Tests
# ============================================================================

class TestProteinEstimation:
    """Tests for protein estimation from item names."""

    def test_grilled_chicken_estimate(self):
        """Grilled chicken should estimate ~35g protein."""
        protein, confidence = estimate_protein_from_name("Grilled Chicken Bowl")
        assert protein is not None
        assert protein >= 30
        assert protein <= 40

    def test_double_chicken_modifier(self):
        """Double chicken should add extra protein."""
        single, _ = estimate_protein_from_name("Chicken Bowl")
        double, _ = estimate_protein_from_name("Double Chicken Bowl")
        assert double > single

    def test_steak_estimate(self):
        """Steak items should estimate ~30g protein."""
        protein, _ = estimate_protein_from_name("Grilled Steak Plate")
        assert protein is not None
        assert protein >= 25
        assert protein <= 35

    def test_salmon_estimate(self):
        """Salmon should estimate ~25g protein."""
        protein, _ = estimate_protein_from_name("Grilled Salmon Fillet")
        assert protein is not None
        assert protein >= 20
        assert protein <= 30

    def test_tofu_estimate(self):
        """Tofu should estimate ~15g protein."""
        protein, _ = estimate_protein_from_name("Crispy Tofu Bowl")
        assert protein is not None
        assert protein >= 10
        assert protein <= 20

    def test_no_protein_keywords(self):
        """Items without protein keywords should return None."""
        protein, confidence = estimate_protein_from_name("French Fries")
        assert protein is None
        assert confidence == "none"

    def test_description_helps_estimate(self):
        """Description should help estimate protein."""
        protein, _ = estimate_protein_from_name(
            "Power Bowl",
            description="Served with grilled chicken breast and quinoa"
        )
        assert protein is not None
        assert protein >= 30

    def test_max_protein_cap(self):
        """Estimated protein should not exceed max cap (60g)."""
        # Even with multiple protein modifiers, should cap at 60g
        protein, _ = estimate_protein_from_name(
            "Triple Double Chicken Extra Protein Mega Bowl"
        )
        assert protein is not None
        assert protein <= 60

    def test_extra_protein_modifier(self):
        """Extra protein modifier should increase estimate."""
        base, _ = estimate_protein_from_name("Chicken Bowl")
        extra, _ = estimate_protein_from_name("Chicken Bowl with Extra Chicken")
        assert extra > base


class TestProcessExtractedItem:
    """Tests for processing extracted menu items."""

    def test_item_with_actual_protein(self):
        """Item with actual protein should be marked as actual source."""
        item = ExtractedMenuItem(
            item_name="Chicken Bowl",
            price=15.99,
            protein_grams=45,
            url="https://example.com/item"
        )
        processed = process_extracted_item(item)
        assert processed.protein_source == "actual"
        assert processed.protein_eligible is True
        assert processed.protein_grams == 45

    def test_item_without_protein_gets_estimated(self):
        """Item without protein should get estimated value."""
        item = ExtractedMenuItem(
            item_name="Grilled Chicken Breast",
            price=18.99,
            protein_grams=None,
            url="https://example.com/item"
        )
        processed = process_extracted_item(item)
        assert processed.protein_source == "estimated"
        assert processed.protein_eligible is True
        assert processed.protein_grams is not None
        assert processed.protein_grams > 0

    def test_item_without_protein_keywords_ineligible(self):
        """Item without protein keywords should be ineligible."""
        item = ExtractedMenuItem(
            item_name="French Fries",
            price=4.99,
            protein_grams=None,
            url="https://example.com/item"
        )
        processed = process_extracted_item(item)
        assert processed.protein_source is None
        assert processed.protein_eligible is False


# ============================================================================
# Price Parsing Tests
# ============================================================================

class TestPriceParsing:
    """Tests for price string parsing."""

    def test_dollar_sign_price(self):
        """Parse price with dollar sign."""
        assert parse_price("$12.99") == 12.99

    def test_no_dollar_sign(self):
        """Parse price without dollar sign."""
        assert parse_price("15.50") == 15.50

    def test_european_format(self):
        """Parse European format (comma decimal)."""
        assert parse_price("12,99") == 12.99

    def test_thousand_separator(self):
        """Parse price with thousand separator."""
        assert parse_price("$1,234.99") == 1234.99

    def test_currency_word(self):
        """Parse price with currency word."""
        assert parse_price("12.99 USD") == 12.99

    def test_empty_string(self):
        """Empty string should return None."""
        assert parse_price("") is None
        assert parse_price(None) is None

    def test_invalid_price(self):
        """Invalid price should return None."""
        assert parse_price("Free") is None
        assert parse_price("N/A") is None


class TestNutritionParsing:
    """Tests for nutrition text parsing."""

    def test_protein_grams(self):
        """Parse protein from nutrition text."""
        assert parse_nutrition_text("45g protein", "protein") == 45
        assert parse_nutrition_text("Protein: 45g", "protein") == 45
        assert parse_nutrition_text("45 grams of protein", "protein") == 45

    def test_calories(self):
        """Parse calories from nutrition text."""
        assert parse_nutrition_text("650 cal", "calories") == 650
        assert parse_nutrition_text("Calories: 620", "calories") == 620

    def test_no_match(self):
        """Return None if nutrient not found."""
        assert parse_nutrition_text("Some description", "protein") is None


# ============================================================================
# Cart Assembly Tests
# ============================================================================

def create_test_restaurant(
    name: str = "Test Restaurant",
    items: List[dict] = None
) -> ExtractedRestaurant:
    """Helper to create test restaurant with items."""
    if items is None:
        items = []

    menu_items = []
    for item_data in items:
        item = ExtractedMenuItem(
            item_name=item_data.get("name", "Test Item"),
            price=item_data.get("price", 10.0),
            protein_grams=item_data.get("protein"),
            protein_source=item_data.get("source", "actual") if item_data.get("protein") else None,
            protein_eligible=item_data.get("protein") is not None,
            url=item_data.get("url", "https://example.com/item")
        )
        menu_items.append(item)

    return ExtractedRestaurant(
        name=name,
        url="https://example.com/restaurant",
        eta_minutes=30,
        items=menu_items
    )


class TestCartAssembly:
    """Tests for cart assembly logic."""

    def test_single_item_cart(self):
        """Single item meeting constraints should be valid cart."""
        restaurant = create_test_restaurant(items=[
            {"name": "High Protein Bowl", "price": 20.0, "protein": 110}
        ])

        carts = find_valid_carts(restaurant, min_protein=100, max_price=30)
        assert len(carts) == 1
        assert carts[0].total_protein_grams == 110
        assert carts[0].total_price == 20.0

    def test_multi_item_cart(self):
        """Multiple items combined should create valid cart."""
        restaurant = create_test_restaurant(items=[
            {"name": "Chicken Bowl", "price": 15.0, "protein": 50},
            {"name": "Extra Chicken", "price": 6.0, "protein": 25},
            {"name": "Side Salad", "price": 5.0, "protein": 30},
        ])

        carts = find_valid_carts(restaurant, min_protein=100, max_price=30)
        assert len(carts) > 0

        # Find a cart meeting constraints
        valid_cart = None
        for cart in carts:
            if cart.total_protein_grams >= 100 and cart.total_price < 30:
                valid_cart = cart
                break

        assert valid_cart is not None

    def test_no_valid_cart_high_price(self):
        """Should return empty if price constraint can't be met."""
        restaurant = create_test_restaurant(items=[
            {"name": "Expensive Bowl", "price": 35.0, "protein": 150}
        ])

        carts = find_valid_carts(restaurant, min_protein=100, max_price=30)
        assert len(carts) == 0

    def test_no_valid_cart_low_protein(self):
        """Should return empty if protein constraint can't be met."""
        restaurant = create_test_restaurant(items=[
            {"name": "Low Protein Item", "price": 10.0, "protein": 20},
            {"name": "Another Low Item", "price": 10.0, "protein": 20},
        ])

        carts = find_valid_carts(restaurant, min_protein=100, max_price=30)
        # Even combining items, can't reach 100g with only 40g total available
        assert len(carts) == 0

    def test_no_eligible_items(self):
        """Should return empty if no eligible items."""
        restaurant = create_test_restaurant(items=[
            {"name": "French Fries", "price": 5.0, "protein": None},
        ])

        carts = find_valid_carts(restaurant, min_protein=100, max_price=30)
        assert len(carts) == 0


# ============================================================================
# Scoring and Ranking Tests
# ============================================================================

class TestScoring:
    """Tests for cart scoring logic."""

    def test_higher_protein_scores_higher(self):
        """Cart with more protein should score higher."""
        cart1 = CartCandidate(
            restaurant="Test",
            restaurant_url="https://example.com",
            cart_items=[CartItem(
                item_name="High Protein",
                price=20.0,
                protein_grams=150,
                protein_source="actual",
                url="https://example.com/item"
            )],
            total_price=20.0,
            total_protein_grams=150,
            eta_minutes=30
        )

        cart2 = CartCandidate(
            restaurant="Test2",
            restaurant_url="https://example.com",
            cart_items=[CartItem(
                item_name="Medium Protein",
                price=20.0,
                protein_grams=110,
                protein_source="actual",
                url="https://example.com/item"
            )],
            total_price=20.0,
            total_protein_grams=110,
            eta_minutes=30
        )

        score1 = score_cart(cart1, min_protein=100, max_price=30)
        score2 = score_cart(cart2, min_protein=100, max_price=30)

        assert score1 > score2

    def test_lower_price_scores_higher(self):
        """Cart with lower price should score higher (same protein)."""
        cart1 = CartCandidate(
            restaurant="Test",
            restaurant_url="https://example.com",
            cart_items=[CartItem(
                item_name="Cheap Option",
                price=15.0,
                protein_grams=110,
                protein_source="actual",
                url="https://example.com/item"
            )],
            total_price=15.0,
            total_protein_grams=110,
            eta_minutes=30
        )

        cart2 = CartCandidate(
            restaurant="Test2",
            restaurant_url="https://example.com",
            cart_items=[CartItem(
                item_name="Expensive Option",
                price=28.0,
                protein_grams=110,
                protein_source="actual",
                url="https://example.com/item"
            )],
            total_price=28.0,
            total_protein_grams=110,
            eta_minutes=30
        )

        score1 = score_cart(cart1, min_protein=100, max_price=30)
        score2 = score_cart(cart2, min_protein=100, max_price=30)

        assert score1 > score2

    def test_estimated_protein_penalty(self):
        """Cart with estimated protein should have penalty."""
        cart_actual = CartCandidate(
            restaurant="Test",
            restaurant_url="https://example.com",
            cart_items=[CartItem(
                item_name="Actual Data",
                price=20.0,
                protein_grams=110,
                protein_source="actual",
                url="https://example.com/item"
            )],
            total_price=20.0,
            total_protein_grams=110,
            includes_estimates=False,
            eta_minutes=30
        )

        cart_estimated = CartCandidate(
            restaurant="Test2",
            restaurant_url="https://example.com",
            cart_items=[CartItem(
                item_name="Estimated Data",
                price=20.0,
                protein_grams=110,
                protein_source="estimated",
                url="https://example.com/item"
            )],
            total_price=20.0,
            total_protein_grams=110,
            includes_estimates=True,
            eta_minutes=30
        )

        score_actual = score_cart(cart_actual, min_protein=100, max_price=30)
        score_estimated = score_cart(cart_estimated, min_protein=100, max_price=30)

        assert score_actual > score_estimated


class TestRanking:
    """Tests for cart ranking logic."""

    def test_rank_returns_top_3(self):
        """Ranking should return at most 3 results."""
        carts = []
        for i in range(10):
            cart = CartCandidate(
                restaurant=f"Restaurant {i}",
                restaurant_url=f"https://example.com/{i}",
                cart_items=[CartItem(
                    item_name=f"Item {i}",
                    price=20.0 + i,
                    protein_grams=110 + i * 5,
                    protein_source="actual",
                    url=f"https://example.com/item/{i}"
                )],
                total_price=20.0 + i,
                total_protein_grams=110 + i * 5,
                eta_minutes=30
            )
            carts.append(cart)

        ranked = rank_carts(carts, min_protein=100, max_price=50, top_n=3)
        assert len(ranked) == 3

    def test_rank_assigns_positions(self):
        """Ranked results should have correct rank positions."""
        carts = [
            CartCandidate(
                restaurant="Best",
                restaurant_url="https://example.com/best",
                cart_items=[CartItem(
                    item_name="Best Item",
                    price=15.0,
                    protein_grams=150,
                    protein_source="actual",
                    url="https://example.com/item/best"
                )],
                total_price=15.0,
                total_protein_grams=150,
                eta_minutes=20
            ),
            CartCandidate(
                restaurant="Good",
                restaurant_url="https://example.com/good",
                cart_items=[CartItem(
                    item_name="Good Item",
                    price=25.0,
                    protein_grams=110,
                    protein_source="actual",
                    url="https://example.com/item/good"
                )],
                total_price=25.0,
                total_protein_grams=110,
                eta_minutes=30
            ),
        ]

        ranked = rank_carts(carts, min_protein=100, max_price=30)

        assert ranked[0].rank == 1
        assert ranked[1].rank == 2
        assert ranked[0].restaurant == "Best"

    def test_rank_empty_list(self):
        """Ranking empty list should return empty."""
        ranked = rank_carts([], min_protein=100, max_price=30)
        assert len(ranked) == 0


# ============================================================================
# Schema Validation Tests
# ============================================================================

class TestSchemaValidation:
    """Tests for Pydantic schema validation."""

    def test_input_defaults(self):
        """Input should have sensible defaults."""
        input_config = FoodDeliveryInput(
            delivery_address="123 Main St, Los Angeles, CA"
        )
        assert input_config.min_protein_grams == 100
        assert input_config.max_price_usd == 30.0
        assert len(input_config.search_terms) > 0
        assert input_config.headless is True

    def test_input_validation(self):
        """Input should validate constraints."""
        # Valid input
        valid = FoodDeliveryInput(
            delivery_address="123 Main St",
            min_protein_grams=150,
            max_price_usd=50.0,
            search_terms=["chicken", "steak"]
        )
        assert valid.min_protein_grams == 150

        # Invalid protein (too high)
        with pytest.raises(Exception):
            FoodDeliveryInput(
                delivery_address="123 Main St",
                min_protein_grams=1000  # Max is 500
            )

    def test_output_success_factory(self):
        """Success factory should create valid output."""
        constraints = FoodDeliveryConstraints(
            min_protein_grams=100,
            max_price_usd=30.0
        )

        from shared.schemas.food_delivery import FoodDeliveryMetadata

        output = FoodDeliveryOutput.success(
            location="123 Main St",
            constraints=constraints,
            search_terms=["chicken"],
            results=[],
            metadata=FoodDeliveryMetadata(
                restaurants_scanned=5,
                items_extracted=50,
                workflow_duration_ms=30000,
                search_terms_used=["chicken"]
            )
        )

        assert output.location == "123 Main St"
        assert output.failure_reason is None

    def test_output_failure_factory(self):
        """Failure factory should create valid output."""
        constraints = FoodDeliveryConstraints(
            min_protein_grams=100,
            max_price_usd=30.0
        )

        output = FoodDeliveryOutput.failure(
            location="123 Main St",
            constraints=constraints,
            search_terms=["chicken"],
            reason="no_restaurants_found_for_search_terms"
        )

        assert len(output.results) == 0
        assert output.failure_reason == "no_restaurants_found_for_search_terms"


# ============================================================================
# Integration Tests (without browser)
# ============================================================================

class TestAggregation:
    """Tests for multi-restaurant aggregation."""

    def test_aggregate_multiple_restaurants(self):
        """Should aggregate carts from multiple restaurants."""
        restaurants = [
            create_test_restaurant(
                name="Restaurant A",
                items=[
                    {"name": "Chicken Bowl A", "price": 18.0, "protein": 110}
                ]
            ),
            create_test_restaurant(
                name="Restaurant B",
                items=[
                    {"name": "Chicken Bowl B", "price": 20.0, "protein": 105}
                ]
            ),
        ]

        constraints = FoodDeliveryConstraints(
            min_protein_grams=100,
            max_price_usd=30
        )

        all_carts, total_items, items_with_protein = aggregate_all_carts(
            restaurants, constraints
        )

        assert len(all_carts) == 2
        assert total_items == 2
        assert items_with_protein == 2

    def test_find_best_attempt_when_no_valid_carts(self):
        """Should find best attempt for debug info."""
        restaurants = [
            create_test_restaurant(
                name="Restaurant A",
                items=[
                    {"name": "Low Protein Item", "price": 15.0, "protein": 40},
                    {"name": "Another Item", "price": 10.0, "protein": 30},
                ]
            ),
        ]

        best = find_best_attempt(restaurants, min_protein=100, max_price=30)

        assert best is not None
        assert best["protein"] == 70  # 40 + 30
        assert best["shortfall"] == "30g protein"


# ============================================================================
# Run tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
