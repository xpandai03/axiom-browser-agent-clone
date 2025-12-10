import os
import json
import logging
from typing import List, Optional
from openai import OpenAI

from shared.schemas.workflow import WorkflowStep

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

WORKFLOW_SYSTEM_PROMPT = """You are a browser automation workflow parser. Convert natural language instructions into a JSON object containing a "steps" array of structured workflow steps.

Each step must be an object with the following structure:
- "action": one of "goto", "click", "type", "upload", "wait", "scroll", "fill_form", "screenshot"
- "selector": CSS selector for the target element (when applicable)
- "url": URL to navigate to (for "goto" action)
- "value": text to type (for "type" action)
- "file": filename to upload (for "upload" action)
- "duration": wait time in ms (for "wait" action)
- "auto_detect": boolean, set to true for automatic selector detection (for "click" and "fill_form")
- "fields": object mapping field names to values (for "fill_form" action)

Use placeholder variables like {{user.first_name}}, {{user.last_name}}, {{user.email}}, {{user.phone}}, {{user.location}}, {{user.linkedin_url}} for user data fields.

SPECIAL INSTRUCTIONS FOR JOB APPLICATIONS:
- When the user says "click apply", "click the apply button", or "apply to this job", use: {"action": "click", "auto_detect": true}
- When the user says "fill the form", "fill the application", or "fill fields with user data", use: {"action": "fill_form", "auto_detect": true, "fields": {"first_name": "{{user.first_name}}", "last_name": "{{user.last_name}}", "email": "{{user.email}}", "phone": "{{user.phone}}", "location": "{{user.location}}", "linkedin_url": "{{user.linkedin_url}}"}}
- Add wait steps (1-2 seconds) after navigation and clicking to allow pages to load

Return a JSON object with a "steps" key containing the array of steps."""

MODEL_NAME = os.environ.get("OPENAI_MODEL", "gpt-4o")


async def parse_instructions_to_steps(instructions: str) -> List[WorkflowStep]:
    """
    Parse natural language instructions into structured workflow steps.

    Args:
        instructions: Natural language description of browser automation workflow

    Returns:
        List of WorkflowStep objects
    """
    try:
        logger.info(f"Parsing instructions: {instructions[:100]}...")

        client = get_openai_client()
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": WORKFLOW_SYSTEM_PROMPT},
                {"role": "user", "content": instructions}
            ],
            response_format={"type": "json_object"}
        )

        content = (response.choices[0].message.content or "").strip()
        logger.debug(f"OpenAI response: {content[:200]}...")

        parsed = json.loads(content)

        # Handle different response formats
        if isinstance(parsed, dict) and "steps" in parsed:
            steps_data = parsed["steps"]
        elif isinstance(parsed, list):
            steps_data = parsed
        else:
            logger.error(f"Unexpected response format: {type(parsed)}")
            return [WorkflowStep(action="goto", url="error://unexpected-format")]

        # Validate and convert to WorkflowStep objects
        steps = []
        for step_data in steps_data:
            try:
                step = WorkflowStep(**step_data)
                steps.append(step)
            except Exception as e:
                logger.warning(f"Skipping invalid step: {step_data}, error: {e}")

        logger.info(f"Parsed {len(steps)} workflow steps")
        return steps

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        return [WorkflowStep(action="goto", url="error://json-parse-failed")]
    except Exception as e:
        logger.error(f"Error parsing instructions: {e}")
        return [WorkflowStep(action="goto", url=f"error://{str(e)}")]
