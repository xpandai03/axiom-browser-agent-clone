import os
from openai import OpenAI

# Using Replit AI Integrations for OpenAI access (no API key required, billed to credits)
# the newest OpenAI model is "gpt-5" which was released August 7, 2025.
# do not change this unless explicitly requested by the user
client = OpenAI(
    api_key=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY"),
    base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
)

SYSTEM_PROMPT = """You are a professional resume tailoring expert. Given a resume and a job description, create a tailored version that:

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

async def generate_tailored_resume(resume_text: str, job_description: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"RESUME:\n{resume_text}\n\nJOB DESCRIPTION:\n{job_description}"}
            ],
            max_completion_tokens=2000
        )
        
        return response.choices[0].message.content or ""
    except Exception as e:
        return f"Error generating tailored resume: {str(e)}"
