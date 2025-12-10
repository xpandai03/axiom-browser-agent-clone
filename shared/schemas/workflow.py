from typing import Optional, Dict, Literal, Union, List
from pydantic import BaseModel, Field
import re


ActionType = Literal["goto", "click", "type", "upload", "wait", "scroll", "extract", "screenshot", "fill_form"]
ApplyMode = Literal["greenhouse_basic"]
ExtractMode = Literal["text", "attribute"]


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
    # Fill form action fields
    fields: Optional[Dict[str, str]] = Field(None, description="Field name to value mapping for fill_form action")
    auto_detect: bool = Field(False, description="Auto-detect selectors for clicks and form fields")

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
            fields=interpolated_fields,
            auto_detect=self.auto_detect,
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
