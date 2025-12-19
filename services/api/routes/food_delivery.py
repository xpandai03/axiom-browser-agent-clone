"""
API routes for the Uber Eats High-Protein Meal Finder workflow.

Endpoints:
- POST /api/food-delivery/run - Execute the food delivery workflow
- POST /api/food-delivery/run-stream - Execute with SSE streaming
"""

import json
import logging
import time
from typing import AsyncGenerator
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from shared.schemas.food_delivery import (
    FoodDeliveryInput,
    FoodDeliveryOutput,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/food-delivery", tags=["food-delivery"])


@router.post("/run", response_model=FoodDeliveryOutput)
async def run_food_delivery_workflow(request: FoodDeliveryInput):
    """
    Execute the Uber Eats high-protein meal finder workflow.

    This workflow:
    1. Opens Uber Eats and sets the delivery address
    2. Searches for high-protein restaurants using provided search terms
    3. Extracts menu items and nutrition data from each restaurant
    4. Assembles valid carts meeting protein (>=100g) and price (<$30) constraints
    5. Ranks and returns top 3 cart options

    Request body:
    - delivery_address: Full US address for delivery
    - min_protein_grams: Minimum protein target (default: 100)
    - max_price_usd: Maximum cart price (default: 30)
    - search_terms: List of search queries (default: ["chicken bowl", ...])
    - max_restaurants: Maximum restaurants to scan (default: 5)
    - max_items_per_restaurant: Maximum items per restaurant (default: 15)
    - headless: Run browser in headless mode (default: true)

    Returns:
    - location: Delivery address used
    - constraints: Applied protein/price constraints
    - results: Top 3 ranked cart options with items, prices, URLs
    - failure_reason: If no valid carts found, explains why
    - metadata: Execution stats (duration, items scanned, etc.)
    """
    try:
        logger.info(f"Starting food delivery workflow for: {request.delivery_address}")
        logger.info(f"Constraints: {request.min_protein_grams}g protein, ${request.max_price_usd} max")
        logger.info(f"Search terms: {request.search_terms}")

        # Lazy import to avoid loading heavy modules at startup
        from ..mcp_client import get_mcp_client
        from ..food_delivery_executor import run_food_delivery_workflow

        # Get MCP client
        client = await get_mcp_client()

        # Execute workflow
        result = await run_food_delivery_workflow(client, request)

        if result.results:
            logger.info(f"Workflow completed: {len(result.results)} valid carts found")
        else:
            logger.warning(f"Workflow completed: no valid carts ({result.failure_reason})")

        return result

    except Exception as e:
        logger.exception(f"Food delivery workflow failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/run-stream")
async def run_food_delivery_stream(
    delivery_address: str = Query(..., description="Full US address for delivery"),
    min_protein_grams: int = Query(100, ge=1, le=500, description="Minimum protein target"),
    max_price_usd: float = Query(30.0, gt=0, le=200, description="Maximum cart price"),
    search_terms: str = Query(
        "chicken bowl,protein bowl,grilled chicken",
        description="Comma-separated search terms"
    ),
    max_restaurants: int = Query(5, ge=1, le=10, description="Max restaurants to scan"),
    headless: bool = Query(True, description="Run browser in headless mode"),
):
    """
    Execute the food delivery workflow with Server-Sent Events (SSE) streaming.

    Events sent:
    - status: Progress updates ("Setting location...", "Searching restaurants...", etc.)
    - restaurant_found: When a restaurant is discovered
    - restaurant_extracted: When menu extraction completes for a restaurant
    - cart_candidate: When a valid cart is assembled
    - complete: Final results
    - error: On any error

    Query Parameters:
    - delivery_address: Full US address (required)
    - min_protein_grams: Minimum protein target (default: 100)
    - max_price_usd: Maximum cart price (default: 30)
    - search_terms: Comma-separated search queries
    - max_restaurants: Maximum restaurants to scan (default: 5)
    - headless: Run browser in headless mode (default: true)
    """
    # Parse search terms
    terms_list = [t.strip() for t in search_terms.split(",") if t.strip()]
    if not terms_list:
        terms_list = ["chicken bowl", "protein bowl", "grilled chicken"]

    async def event_generator() -> AsyncGenerator[str, None]:
        start_time = time.time()

        try:
            yield f"event: status\ndata: {json.dumps({'message': 'Initializing browser...'})}\n\n"

            # Build input config
            input_config = FoodDeliveryInput(
                delivery_address=delivery_address,
                min_protein_grams=min_protein_grams,
                max_price_usd=max_price_usd,
                search_terms=terms_list,
                max_restaurants=max_restaurants,
                headless=headless,
            )

            yield f"event: status\ndata: {json.dumps({'message': f'Setting delivery location: {delivery_address}'})}\n\n"

            # Lazy import
            from ..mcp_client import get_mcp_client
            from ..food_delivery_executor import FoodDeliveryExecutor

            client = await get_mcp_client()
            executor = FoodDeliveryExecutor(client, headless=headless)

            yield f"event: status\ndata: {json.dumps({'message': 'Navigating to Uber Eats...'})}\n\n"

            # Execute workflow (this is a simplified streaming version)
            # For full streaming, the executor would need to yield events
            result = await executor.execute(input_config)

            # Send completion event
            duration_ms = int((time.time() - start_time) * 1000)

            complete_data = {
                "success": len(result.results) > 0,
                "results_count": len(result.results),
                "duration_ms": duration_ms,
                "failure_reason": result.failure_reason,
                "results": [r.model_dump() for r in result.results],
            }

            yield f"event: complete\ndata: {json.dumps(complete_data, default=str)}\n\n"

        except Exception as e:
            logger.exception(f"Streaming workflow failed: {e}")
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@router.get("/test")
async def test_endpoint():
    """Test endpoint to verify the food delivery router is working."""
    return {
        "status": "ok",
        "message": "Food delivery endpoint is active",
        "endpoints": [
            "POST /api/food-delivery/run",
            "GET /api/food-delivery/run-stream",
        ]
    }
