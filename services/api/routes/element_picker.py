"""Element picker API routes for visual selector."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
import logging

from ..mcp_runtime import PlaywrightRuntime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/element-picker", tags=["element-picker"])

# Shared runtime instance for element picker
_picker_runtime: Optional[PlaywrightRuntime] = None


async def get_picker_runtime() -> PlaywrightRuntime:
    """Get or create a dedicated runtime for element picker."""
    global _picker_runtime
    if _picker_runtime is None:
        _picker_runtime = PlaywrightRuntime()
    return _picker_runtime


class PickerRequest(BaseModel):
    url: str


class BoundingBox(BaseModel):
    x: int
    y: int
    width: int
    height: int


class ElementInfo(BaseModel):
    selector: str
    tag: str
    text: str
    placeholder: Optional[str] = None
    bbox: BoundingBox


class PickerResponse(BaseModel):
    success: bool
    url: str
    screenshot_base64: str
    elements: List[Dict]
    viewport: Dict[str, int]
    element_count: int
    error: Optional[str] = None


@router.post("/load", response_model=PickerResponse)
async def load_page_for_picker(request: PickerRequest):
    """
    Navigate to URL and return screenshot + clickable elements with bounding boxes.

    Used by the visual element picker to display a clickable overlay.
    """
    try:
        runtime = await get_picker_runtime()

        # Navigate to URL
        logger.info(f"Element picker: navigating to {request.url}")
        nav_result = await runtime.navigate(request.url)
        if not nav_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Failed to navigate: {nav_result.get('error', 'Unknown error')}"
            )

        # Wait for page to stabilize
        await runtime.wait(1500)

        # Get elements with bounding boxes
        elements_result = await runtime.get_elements_with_boxes()
        if not elements_result.get("success"):
            raise HTTPException(status_code=500, detail="Failed to extract elements")

        # Take screenshot
        screenshot_result = await runtime.screenshot()
        if not screenshot_result.get("success"):
            raise HTTPException(status_code=500, detail="Failed to capture screenshot")

        elements = elements_result.get("elements", [])

        return PickerResponse(
            success=True,
            url=request.url,
            screenshot_base64=screenshot_result.get("screenshot_base64", ""),
            elements=elements,
            viewport={"width": 1280, "height": 720},
            element_count=len(elements)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Element picker error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ClickAndUpdateRequest(BaseModel):
    selector: str


@router.post("/click-and-update")
async def click_and_get_new_state(request: ClickAndUpdateRequest):
    """
    Click an element and return new screenshot + elements.
    Useful for navigating within the picker.
    """
    try:
        runtime = await get_picker_runtime()

        # Click the element
        click_result = await runtime.click(request.selector)
        if not click_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Click failed: {click_result.get('error', 'Unknown error')}"
            )

        # Wait for page update
        await runtime.wait(1500)

        # Get new state
        elements_result = await runtime.get_elements_with_boxes()
        screenshot_result = await runtime.screenshot()

        return {
            "success": True,
            "screenshot_base64": screenshot_result.get("screenshot_base64", ""),
            "elements": elements_result.get("elements", []),
            "element_count": len(elements_result.get("elements", []))
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Click and update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ScrollRequest(BaseModel):
    direction: str = "down"  # "up" or "down"
    amount: int = 500  # pixels


@router.post("/scroll")
async def scroll_picker_browser(request: ScrollRequest):
    """
    Scroll the picker browser and return new screenshot + elements.

    Used to explore elements below/above the current viewport.
    """
    try:
        runtime = await get_picker_runtime()

        if runtime._page is None:
            raise HTTPException(
                status_code=400,
                detail="No page loaded. Call /load first."
            )

        # Scroll the page
        scroll_result = await runtime.scroll(request.direction, request.amount)
        if not scroll_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"Scroll failed: {scroll_result.get('error', 'Unknown error')}"
            )

        # Wait briefly for any lazy-loaded content
        await runtime.wait(500)

        # Get new elements and screenshot
        elements_result = await runtime.get_elements_with_boxes()
        screenshot_result = await runtime.screenshot()

        elements = elements_result.get("elements", [])

        return {
            "success": True,
            "screenshot_base64": screenshot_result.get("screenshot_base64", ""),
            "elements": elements,
            "element_count": len(elements),
            "scroll_direction": request.direction,
            "scroll_amount": request.amount
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Scroll picker error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/close")
async def close_picker_browser():
    """Close the picker browser instance."""
    global _picker_runtime
    try:
        if _picker_runtime:
            await _picker_runtime.close()
            _picker_runtime = None
        return {"success": True, "message": "Picker browser closed"}
    except Exception as e:
        logger.exception(f"Error closing picker browser: {e}")
        return {"success": False, "error": str(e)}
