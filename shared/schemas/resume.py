from typing import Optional
from pydantic import BaseModel, Field


class ResumeRequest(BaseModel):
    """Request to tailor a resume for a job description."""

    resume_text: str = Field(..., min_length=1, description="Original resume text")
    job_description: str = Field(..., min_length=1, description="Target job description")
    max_tokens: int = Field(2000, ge=100, le=4000, description="Max tokens for AI response")


class TailoredResume(BaseModel):
    """Response containing tailored resume content."""

    content: str = Field(..., description="Tailored resume content in markdown")
    success: bool = Field(True, description="Whether tailoring was successful")
    error: Optional[str] = Field(None, description="Error message if tailoring failed")
