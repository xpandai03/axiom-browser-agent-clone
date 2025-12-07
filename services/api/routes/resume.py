import logging
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from shared.schemas.resume import ResumeRequest, TailoredResume
from shared.ai import generate_tailored_resume

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/resume", tags=["resume"])


@router.post("/tailor", response_model=TailoredResume)
async def tailor_resume(
    job_description: str = Form(..., description="Target job description"),
    resume: Optional[UploadFile] = File(None, description="Resume file"),
    resume_text: Optional[str] = Form(None, description="Resume text (alternative to file)"),
):
    """
    Tailor a resume for a specific job description.

    Either upload a resume file or provide resume_text.
    """
    # Get resume text from file or form
    if resume:
        try:
            content = await resume.read()
            resume_content = content.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"Failed to read resume file: {e}")
            raise HTTPException(status_code=400, detail="Failed to read resume file")
    elif resume_text:
        resume_content = resume_text
    else:
        raise HTTPException(
            status_code=400,
            detail="Either resume file or resume_text is required"
        )

    # Generate tailored resume
    result = await generate_tailored_resume(
        resume_text=resume_content,
        job_description=job_description,
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    return result
