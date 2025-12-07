import os
import logging
from typing import Optional
from openai import OpenAI

from shared.schemas.resume import TailoredResume

logger = logging.getLogger(__name__)

# Lazy initialization of OpenAI client
_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    """Get or create the OpenAI client."""
    global _client
    if _client is None:
        api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key not set. Set OPENAI_API_KEY environment variable.")
        _client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL"),
        )
    return _client

RESUME_SYSTEM_PROMPT = """You are a professional resume tailoring expert. Given a resume and a job description, create a tailored version that:

1. Highlights relevant experience and skills that match the job requirements
2. Rewrites bullet points to emphasize accomplishments relevant to the role
3. Creates a compelling professional summary targeting this specific position
4. Suggests keywords from the job description to include

Format your response as:

## TAILORED PROFESSIONAL SUMMARY
[A 2-3 sentence summary tailored to this role]

## KEY SKILLS MATCH
[Bullet list of skills from the resume that match job requirements]

## ENHANCED EXPERIENCE BULLETS
[Rewritten bullet points from experience section, optimized for this role]

## KEYWORDS TO INCLUDE
[List of important keywords from the job description to incorporate]

## ADDITIONAL RECOMMENDATIONS
[Any other suggestions to improve the application]"""

MODEL_NAME = os.environ.get("OPENAI_MODEL", "gpt-4o")


async def generate_tailored_resume(
    resume_text: str,
    job_description: str,
    max_tokens: int = 2000
) -> TailoredResume:
    """
    Generate a tailored resume based on job description.

    Args:
        resume_text: Original resume content
        job_description: Target job description
        max_tokens: Maximum tokens for AI response

    Returns:
        TailoredResume object with tailored content
    """
    try:
        logger.info("Generating tailored resume...")

        client = get_openai_client()
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": RESUME_SYSTEM_PROMPT},
                {"role": "user", "content": f"RESUME:\n{resume_text}\n\nJOB DESCRIPTION:\n{job_description}"}
            ],
            max_completion_tokens=max_tokens
        )

        content = response.choices[0].message.content or ""
        logger.info("Successfully generated tailored resume")

        return TailoredResume(content=content, success=True)

    except Exception as e:
        logger.error(f"Error generating tailored resume: {e}")
        return TailoredResume(
            content="",
            success=False,
            error=f"Error generating tailored resume: {str(e)}"
        )
