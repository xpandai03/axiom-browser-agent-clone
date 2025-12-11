from typing import Optional, Dict, Literal, Union, List
from pydantic import BaseModel, Field
import re


ActionType = Literal[
    "goto", "click", "type", "upload", "wait", "scroll",
    "extract", "screenshot", "fill_form", "click_first_job",
    "extract_job_links", "loop_jobs",  # Multi-job scraping actions
    # Phase 7: Hard-Site Scraping actions
    "extract_links", "extract_text", "extract_attributes",
    "scroll_until", "random_scroll", "detect_block",
    "wait_for_selector", "loop_urls"
]
ApplyMode = Literal["greenhouse_basic"]
ExtractMode = Literal["text", "attribute"]
ScrollMode = Literal["pixels", "to_element", "until_text"]
ScrollUntilCondition = Literal["selector_visible", "end_of_page", "count"]
WaitForState = Literal["visible", "attached", "hidden"]


class WorkflowStep(BaseModel):
    """A single step in a browser automation workflow."""

    action: ActionType
    selector: Optional[str] = Field(None, description="CSS selector for target element")
    url: Optional[str] = Field(None, description="URL for goto action")
    value: Optional[str] = Field(None, description="Text to type for type action")
    file: Optional[str] = Field(None, description="Filename for upload action")
    duration: Optional[int] = Field(None, ge=0, description="Wait duration in milliseconds")
    # Extract action fields
    attribute: Optional[str] = Field(None, description="Attribute name to extract (for extract action)")
    extract_mode: ExtractMode = Field("text", description="Extract mode: 'text' or 'attribute'")
    label: Optional[str] = Field(None, description="Label for extracted data (used in logs and data aggregation)")
    # Fill form action fields
    fields: Optional[Dict[str, str]] = Field(None, description="Field name to value mapping for fill_form action")
    auto_detect: bool = Field(False, description="Auto-detect selectors for clicks and form fields")
    # Enhanced scroll fields
    scroll_mode: ScrollMode = Field("pixels", description="Scroll mode: pixels, to_element, or until_text")
    scroll_direction: Optional[str] = Field("down", description="Scroll direction: up or down (for pixels mode)")
    scroll_amount: Optional[int] = Field(None, ge=0, description="Scroll amount in pixels (for pixels mode)")
    scroll_text: Optional[str] = Field(None, description="Text to scroll until visible (for until_text mode)")
    # Multi-job scraping fields (for loop_jobs action)
    max_jobs: Optional[int] = Field(None, ge=1, le=50, description="Max jobs to process (for loop_jobs)")
    job_url_source: Optional[str] = Field(None, description="Label of extracted job URLs to iterate (for loop_jobs)")

    # Phase 7: Hard-Site Scraping fields
    # extract_links fields
    filter_pattern: Optional[str] = Field(None, description="Regex pattern to filter URLs (for extract_links)")
    include_text: bool = Field(True, description="Include link text alongside href (for extract_links)")

    # extract_text fields
    clean_whitespace: bool = Field(True, description="Collapse whitespace in extracted text")
    max_length: Optional[int] = Field(None, ge=1, description="Truncate extracted text to N chars")

    # extract_attributes fields
    attributes: Optional[List[str]] = Field(None, description="List of attributes to extract (for extract_attributes)")

    # scroll_until fields
    scroll_condition: ScrollUntilCondition = Field("count", description="Scroll until condition: selector_visible, end_of_page, or count")
    max_scrolls: Optional[int] = Field(20, ge=1, le=100, description="Max scroll iterations (safety limit)")
    scroll_delay_ms: Optional[int] = Field(None, ge=100, description="Delay between scrolls in ms")

    # random_scroll fields
    min_scrolls: Optional[int] = Field(2, ge=1, description="Min scroll actions (for random_scroll)")
    max_delay_ms: Optional[int] = Field(1200, ge=100, description="Max delay between scrolls in ms")
    min_delay_ms: Optional[int] = Field(300, ge=50, description="Min delay between scrolls in ms")

    # detect_block fields
    abort_on_block: bool = Field(False, description="Abort workflow if bot block detected")

    # wait_for_selector fields
    fallback_selectors: Optional[List[str]] = Field(None, description="Fallback selectors if primary fails")
    timeout_ms: Optional[int] = Field(10000, ge=1000, description="Timeout per selector in ms")
    wait_state: WaitForState = Field("visible", description="State to wait for: visible, attached, hidden")

    # loop_urls fields
    source: Optional[str] = Field(None, description="Label of extracted URLs to iterate (for loop_urls)")
    max_items: Optional[int] = Field(10, ge=1, le=100, description="Max URLs to process (for loop_urls)")
    delay_between_ms: Optional[int] = Field(2000, ge=500, description="Delay between URL visits in ms")
    extract_fields: Optional[List[Dict[str, str]]] = Field(None, description="Fields to extract from each page (for loop_urls)")

    def interpolate(self, user_data: Dict[str, str]) -> "WorkflowStep":
        """Replace {{user.x}} placeholders with actual values from user_data."""
        def replace_placeholders(text: Optional[str]) -> Optional[str]:
            if not text:
                return text
            pattern = r"\{\{user\.(\w+)\}\}"
            def replacer(match):
                key = match.group(1)
                return user_data.get(key, match.group(0))
            return re.sub(pattern, replacer, text)

        # Interpolate fields dict if present
        interpolated_fields = None
        if self.fields:
            interpolated_fields = {
                k: replace_placeholders(v) for k, v in self.fields.items()
            }

        return WorkflowStep(
            action=self.action,
            selector=replace_placeholders(self.selector),
            url=replace_placeholders(self.url),
            value=replace_placeholders(self.value),
            file=replace_placeholders(self.file),
            duration=self.duration,
            attribute=replace_placeholders(self.attribute),
            extract_mode=self.extract_mode,
            label=self.label,
            fields=interpolated_fields,
            auto_detect=self.auto_detect,
            # Enhanced scroll fields
            scroll_mode=self.scroll_mode,
            scroll_direction=self.scroll_direction,
            scroll_amount=self.scroll_amount,
            scroll_text=replace_placeholders(self.scroll_text),
            # Multi-job scraping fields
            max_jobs=self.max_jobs,
            job_url_source=self.job_url_source,
            # Phase 7: Hard-Site Scraping fields
            filter_pattern=self.filter_pattern,
            include_text=self.include_text,
            clean_whitespace=self.clean_whitespace,
            max_length=self.max_length,
            attributes=self.attributes,
            scroll_condition=self.scroll_condition,
            max_scrolls=self.max_scrolls,
            scroll_delay_ms=self.scroll_delay_ms,
            min_scrolls=self.min_scrolls,
            max_delay_ms=self.max_delay_ms,
            min_delay_ms=self.min_delay_ms,
            abort_on_block=self.abort_on_block,
            fallback_selectors=self.fallback_selectors,
            timeout_ms=self.timeout_ms,
            wait_state=self.wait_state,
            source=self.source,
            max_items=self.max_items,
            delay_between_ms=self.delay_between_ms,
            extract_fields=self.extract_fields,
        )


class WorkflowRequest(BaseModel):
    """Request to execute a workflow from natural language instructions."""

    instructions: str = Field(..., min_length=1, description="Natural language workflow instructions")
    user_data: Optional[Dict[str, str]] = Field(
        default=None,
        description="User data for placeholder interpolation (e.g., name, email, phone)"
    )
    job_description: Optional[str] = Field(None, description="Job description for resume tailoring")
    resume_text: Optional[str] = Field(None, description="Resume text for tailoring")
    headless: bool = Field(True, description="Run browser in headless mode")
    apply_mode: Optional[ApplyMode] = Field(None, description="Apply mode hint for job applications (e.g., greenhouse_basic)")
