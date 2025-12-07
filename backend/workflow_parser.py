import os
import json
from openai import OpenAI

# Using Replit AI Integrations for OpenAI access (no API key required, billed to credits)
# the newest OpenAI model is "gpt-5" which was released August 7, 2025.
# do not change this unless explicitly requested by the user
client = OpenAI(
    api_key=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY"),
    base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
)

SYSTEM_PROMPT = """You are a browser automation workflow parser. Convert natural language instructions into a JSON object containing a "steps" array of structured workflow steps.

Each step must be an object with the following structure:
- "action": one of "goto", "click", "type", "upload", "wait", "scroll"
- "selector": CSS selector for the target element (when applicable)
- "url": URL to navigate to (for "goto" action)
- "value": text to type (for "type" action)
- "file": filename to upload (for "upload" action)
- "duration": wait time in ms (for "wait" action)

Use placeholder variables like {{user.name}}, {{user.email}}, {{user.phone}} for user data fields.

Return a JSON object with a "steps" key containing the array of steps."""

async def parse_instructions_to_steps(instructions: str) -> list:
    try:
        response = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": instructions}
            ],
            response_format={"type": "json_object"}
        )
        
        content = (response.choices[0].message.content or "").strip()
        
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "steps" in parsed:
            return parsed["steps"]
        elif isinstance(parsed, list):
            return parsed
        else:
            return [{"action": "error", "message": "Unexpected response format"}]
    except json.JSONDecodeError:
        return [{"action": "error", "message": "Failed to parse workflow steps"}]
    except Exception as e:
        return [{"action": "error", "message": str(e)}]
