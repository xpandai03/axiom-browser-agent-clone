"""
Pydantic schemas for the Uber Eats High-Protein Meal Finder agent.

This module defines input/output contracts for the food delivery workflow
that searches Uber Eats for meal carts meeting protein and budget constraints.
"""

from typing import Optional, List, Literal
from pydantic import BaseModel, Field, validator
from datetime import datetime


# ============================================================================
# Input Schemas
# ============================================================================

class FoodDeliveryInput(BaseModel):
    """Input configuration for the food delivery workflow."""

    delivery_address: str = Field(
        ...,
        description="Full delivery address (US format)",
        example="3333 S La Cienaga Blvd, Los Angeles, CA 90016"
    )
    min_protein_grams: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Minimum total protein target in grams"
    )
    max_price_usd: float = Field(
        default=30.0,
        gt=0,
        le=200,
        description="Maximum cart price in USD (before tax/tip)"
    )
    search_terms: List[str] = Field(
        default=["chicken bowl", "protein bowl", "grilled chicken", "burrito bowl"],
        min_items=1,
        max_items=6,
        description="Search terms to find high-protein restaurants"
    )
    max_restaurants: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum number of restaurants to scan"
    )
    max_items_per_restaurant: int = Field(
        default=15,
        ge=1,
        le=30,
        description="Maximum menu items to extract per restaurant"
    )
    headless: bool = Field(
        default=True,
        description="Run browser in headless mode"
    )

    @validator('search_terms', each_item=True)
    def validate_search_term(cls, v):
        if not v or len(v.strip()) < 2:
            raise ValueError("Search term must be at least 2 characters")
        return v.strip()


# ============================================================================
# Extraction Schemas (intermediate data structures)
# ============================================================================

ProteinSource = Literal["actual", "estimated", None]


class ExtractedMenuItem(BaseModel):
    """A single menu item extracted from a restaurant page."""

    item_name: str = Field(..., description="Name of the menu item")
    price: float = Field(..., ge=0, description="Price in USD")
    protein_grams: Optional[int] = Field(
        None,
        ge=0,
        le=200,
        description="Protein content in grams (null if not visible)"
    )
    protein_source: Optional[ProteinSource] = Field(
        None,
        description="Source of protein data: 'actual' (from page), 'estimated' (from name), or null"
    )
    protein_eligible: bool = Field(
        default=False,
        description="Whether this item can be used in cart assembly"
    )
    calories: Optional[int] = Field(
        None,
        ge=0,
        le=5000,
        description="Calorie content (null if not visible)"
    )
    description: Optional[str] = Field(
        None,
        max_length=500,
        description="Item description (used for protein estimation)"
    )
    url: str = Field(..., description="Direct URL to the item on Uber Eats")

    @property
    def protein_per_dollar(self) -> float:
        """Calculate protein efficiency (grams per dollar)."""
        if self.protein_grams and self.price > 0:
            return self.protein_grams / self.price
        return 0.0


class ExtractedRestaurant(BaseModel):
    """A restaurant with its extracted menu items."""

    name: str = Field(..., description="Restaurant name")
    url: str = Field(..., description="Restaurant page URL on Uber Eats")
    eta_minutes: Optional[int] = Field(
        None,
        ge=0,
        le=180,
        description="Estimated delivery time in minutes"
    )
    items: List[ExtractedMenuItem] = Field(
        default_factory=list,
        description="Extracted menu items"
    )
    extraction_success: bool = Field(
        default=True,
        description="Whether menu extraction succeeded"
    )
    extraction_error: Optional[str] = Field(
        None,
        description="Error message if extraction failed"
    )

    @property
    def eligible_items(self) -> List[ExtractedMenuItem]:
        """Return only items eligible for cart assembly."""
        return [item for item in self.items if item.protein_eligible]


# ============================================================================
# Cart Assembly Schemas
# ============================================================================

class CartItem(BaseModel):
    """A menu item included in a cart."""

    item_name: str = Field(..., description="Name of the menu item")
    price: float = Field(..., ge=0, description="Price in USD")
    protein_grams: int = Field(..., ge=0, description="Protein content in grams")
    protein_source: ProteinSource = Field(..., description="Source of protein data")
    calories: Optional[int] = Field(None, ge=0, description="Calorie content")
    url: str = Field(..., description="Direct URL to the item")


class CartCandidate(BaseModel):
    """A potential cart combination for ranking."""

    restaurant: str = Field(..., description="Restaurant name")
    restaurant_url: str = Field(..., description="Restaurant page URL")
    cart_items: List[CartItem] = Field(..., min_items=1, description="Items in the cart")
    total_price: float = Field(..., ge=0, description="Total cart price in USD")
    total_protein_grams: int = Field(..., ge=0, description="Total protein in grams")
    includes_estimates: bool = Field(
        default=False,
        description="Whether any protein values are estimated"
    )
    eta_minutes: Optional[int] = Field(None, description="Estimated delivery time")
    score: float = Field(default=0.0, description="Ranking score")
    reason: Optional[str] = Field(None, description="Why this cart was selected")

    @property
    def item_count(self) -> int:
        return len(self.cart_items)

    @property
    def protein_per_dollar(self) -> float:
        if self.total_price > 0:
            return self.total_protein_grams / self.total_price
        return 0.0

    @property
    def actual_protein_ratio(self) -> float:
        """Ratio of protein from actual (not estimated) sources."""
        actual = sum(
            item.protein_grams
            for item in self.cart_items
            if item.protein_source == "actual"
        )
        return actual / self.total_protein_grams if self.total_protein_grams > 0 else 0.0


# ============================================================================
# Output Schemas
# ============================================================================

class FoodDeliveryConstraints(BaseModel):
    """Constraints that were applied to the search."""
    min_protein_grams: int
    max_price_usd: float


class RankedCartResult(BaseModel):
    """A ranked cart result in the final output."""

    rank: int = Field(..., ge=1, description="Ranking position (1 = best)")
    restaurant: str = Field(..., description="Restaurant name")
    restaurant_url: str = Field(..., description="Restaurant page URL")
    cart_items: List[CartItem] = Field(..., description="Items in the cart")
    total_price: float = Field(..., ge=0, description="Total cart price in USD")
    total_protein_grams: int = Field(..., ge=0, description="Total protein in grams")
    includes_estimates: bool = Field(
        default=False,
        description="Whether any protein values are estimated"
    )
    eta_minutes: Optional[int] = Field(None, description="Estimated delivery time")
    score: float = Field(..., description="Ranking score")
    reason: str = Field(..., description="Why this cart was selected")


class DebugInfo(BaseModel):
    """Debug information for troubleshooting."""

    restaurants_scanned: int = Field(default=0, description="Number of restaurants scanned")
    restaurants_skipped: int = Field(default=0, description="Number of restaurants that failed")
    items_extracted: int = Field(default=0, description="Total menu items extracted")
    items_with_protein_data: int = Field(default=0, description="Items with actual protein data")
    items_with_estimates: int = Field(default=0, description="Items with estimated protein")
    candidate_carts_generated: int = Field(default=0, description="Total cart combinations tried")
    valid_carts_found: int = Field(default=0, description="Carts meeting constraints")
    best_attempt: Optional[dict] = Field(
        None,
        description="Best cart attempt if no valid carts found"
    )
    blocked_at_step: Optional[str] = Field(
        None,
        description="Step where bot detection was triggered"
    )
    debug_screenshots: Optional[List[dict]] = Field(
        None,
        description="List of debug screenshots [{label, base64}]"
    )
    nav_attempts: int = Field(default=1, description="Number of navigation attempts")
    page_state_detected: Optional[str] = Field(
        None,
        description="Page state detected (healthy, navigation_stalled, bot_challenge_detected, etc.)"
    )
    # IP verification fields
    outbound_ip: Optional[str] = Field(None, description="Outbound IP from ipify check")
    ip_is_datacenter: Optional[bool] = Field(None, description="True if IP looks like datacenter")


class FoodDeliveryMetadata(BaseModel):
    """Execution metadata."""

    restaurants_scanned: int = Field(..., description="Number of restaurants scanned")
    items_extracted: int = Field(..., description="Total menu items extracted")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="When the workflow completed"
    )
    workflow_duration_ms: int = Field(..., ge=0, description="Total execution time in ms")
    search_terms_used: List[str] = Field(..., description="Search terms that were executed")


FailureReason = Literal[
    "uber_eats_unavailable",
    "address_not_serviceable",
    "no_restaurants_found_for_search_terms",
    "nutrition_data_not_available",
    "no_carts_meet_protein_and_price_constraints",
    "bot_detection_triggered",
    "rate_limited",
    "unknown_error",
    # Page state failures (cloud blocking)
    "navigation_stalled",
    "bot_challenge_detected",
    "geo_selector_detected",
    "page_degraded",
]


class FoodDeliveryOutput(BaseModel):
    """Final output of the food delivery workflow."""

    location: str = Field(..., description="Delivery address used")
    constraints: FoodDeliveryConstraints = Field(..., description="Applied constraints")
    search_terms_used: List[str] = Field(..., description="Search terms executed")
    results: List[RankedCartResult] = Field(
        default_factory=list,
        max_items=3,
        description="Top 3 ranked cart options"
    )
    failure_reason: Optional[FailureReason] = Field(
        None,
        description="Reason for failure if no results"
    )
    metadata: Optional[FoodDeliveryMetadata] = Field(
        None,
        description="Execution metadata"
    )
    debug: Optional[DebugInfo] = Field(
        None,
        description="Debug information"
    )

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    @classmethod
    def success(
        cls,
        location: str,
        constraints: FoodDeliveryConstraints,
        search_terms: List[str],
        results: List[RankedCartResult],
        metadata: FoodDeliveryMetadata,
        debug: Optional[DebugInfo] = None
    ) -> "FoodDeliveryOutput":
        """Create a successful output."""
        return cls(
            location=location,
            constraints=constraints,
            search_terms_used=search_terms,
            results=results,
            metadata=metadata,
            debug=debug
        )

    @classmethod
    def failure(
        cls,
        location: str,
        constraints: FoodDeliveryConstraints,
        search_terms: List[str],
        reason: FailureReason,
        debug: Optional[DebugInfo] = None
    ) -> "FoodDeliveryOutput":
        """Create a failure output."""
        return cls(
            location=location,
            constraints=constraints,
            search_terms_used=search_terms,
            results=[],
            failure_reason=reason,
            debug=debug
        )
