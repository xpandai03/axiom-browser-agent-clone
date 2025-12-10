"""
Greenhouse-specific helpers for Easy Apply automation.

This module provides selector lists and auto-detection logic for
Greenhouse job application forms.
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SelectorResult:
    """Result of selector detection."""
    found: bool
    selector: Optional[str] = None
    method: str = "none"  # "direct", "fallback", "auto"


# Greenhouse Apply Button Selectors (ordered by specificity)
GREENHOUSE_APPLY_SELECTORS = [
    # Most specific - Greenhouse's standard apply link
    "a[href*='#app']",
    # Text-based selectors
    "a:has-text('Apply for this job')",
    "a:has-text('Apply now')",
    "a:has-text('Apply')",
    "button:has-text('Apply for this job')",
    "button:has-text('Apply now')",
    "button:has-text('Apply')",
    # Class-based (Greenhouse uses these)
    ".btn-apply",
    "[data-test='apply-button']",
]

# Greenhouse Form Field Selectors - ID-based first (Greenhouse uses IDs)
GREENHOUSE_FIELD_SELECTORS: Dict[str, List[str]] = {
    "first_name": [
        "input#first_name",
        "input[name='first_name']",
        "input[autocomplete='given-name']",
        "input[placeholder*='First']",
        "input[aria-label*='First name']",
    ],
    "last_name": [
        "input#last_name",
        "input[name='last_name']",
        "input[autocomplete='family-name']",
        "input[placeholder*='Last']",
        "input[aria-label*='Last name']",
    ],
    "email": [
        "input#email",
        "input[name='email']",
        "input[type='email']",
        "input[autocomplete='email']",
        "input[placeholder*='Email']",
        "input[aria-label*='Email']",
    ],
    "phone": [
        "input#phone",
        "input[name='phone']",
        "input[type='tel']",
        "input[autocomplete='tel']",
        "input[placeholder*='Phone']",
        "input[aria-label*='Phone']",
    ],
    "location": [
        "input#location",
        "input[name='location']",
        "input[autocomplete='address-level2']",
        "input[placeholder*='Location']",
        "input[placeholder*='City']",
        "input[aria-label*='Location']",
    ],
    "linkedin_url": [
        "input[id*='linkedin']",
        "input[name*='linkedin']",
        "input[name*='LinkedIn']",
        "input[placeholder*='LinkedIn']",
        "input[placeholder*='linkedin']",
        "input[aria-label*='LinkedIn']",
    ],
    "resume": [
        "input[type='file'][id*='resume']",
        "input#resume",
        "input[type='file'][name*='resume']",
        "input[type='file'][accept*='.pdf']",
        "input[type='file']",
    ],
}

# Field name aliases (map user_data keys to Greenhouse field names)
FIELD_ALIASES: Dict[str, str] = {
    "name": "first_name",  # If user provides "name", try first_name
    "firstname": "first_name",
    "lastname": "last_name",
    "linkedin": "linkedin_url",
    "phone_number": "phone",
    "city": "location",
}


def normalize_field_name(field_name: str) -> str:
    """Normalize field name using aliases."""
    normalized = field_name.lower().strip().replace(" ", "_").replace("-", "_")
    return FIELD_ALIASES.get(normalized, normalized)


def get_apply_button_selectors() -> List[str]:
    """Get ordered list of Apply button selectors to try."""
    return GREENHOUSE_APPLY_SELECTORS.copy()


def get_field_selectors(field_name: str) -> List[str]:
    """
    Get ordered list of selectors to try for a given field.

    Args:
        field_name: The field to get selectors for (e.g., "first_name", "email")

    Returns:
        List of CSS selectors to try, in order of preference
    """
    normalized = normalize_field_name(field_name)
    selectors = GREENHOUSE_FIELD_SELECTORS.get(normalized, [])

    if not selectors:
        # Fallback: generate generic selectors
        logger.warning(f"No predefined selectors for field: {field_name}, using generic fallbacks")
        selectors = [
            f"input[name='{field_name}']",
            f"input[id='{field_name}']",
            f"input[placeholder*='{field_name}']",
        ]

    return selectors


def get_default_user_data_fields() -> Dict[str, str]:
    """
    Get the default field mapping for fill_form with user data placeholders.

    Returns:
        Dict mapping field names to {{user.x}} placeholders
    """
    return {
        "first_name": "{{user.first_name}}",
        "last_name": "{{user.last_name}}",
        "email": "{{user.email}}",
        "phone": "{{user.phone}}",
        "location": "{{user.location}}",
        "linkedin_url": "{{user.linkedin_url}}",
    }


async def try_selectors(
    client,
    selectors: List[str],
    action: str = "click",
    value: str = None,
    timeout_ms: int = 5000
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Try multiple selectors until one works.

    Args:
        client: MCP client instance
        selectors: List of selectors to try
        action: "click" or "fill"
        value: Value to fill (for fill action)
        timeout_ms: Timeout per selector attempt

    Returns:
        Tuple of (success, selector_used, error_message)
    """
    last_error = None

    for selector in selectors:
        try:
            logger.debug(f"Trying selector: {selector}")

            if action == "click":
                result = await client.click(selector)
            elif action == "fill":
                result = await client.fill(selector, value or "")
            else:
                logger.error(f"Unknown action: {action}")
                continue

            if result.success:
                logger.info(f"Selector worked: {selector}")
                return True, selector, None
            else:
                last_error = result.error
                logger.debug(f"Selector failed: {selector} - {result.error}")

        except Exception as e:
            last_error = str(e)
            logger.debug(f"Selector exception: {selector} - {e}")

    return False, None, last_error or "No selector worked"


def build_greenhouse_workflow_steps(
    job_url: str,
    user_data: Dict[str, str] = None,
    include_upload: bool = False
) -> List[dict]:
    """
    Build a complete Greenhouse Easy Apply workflow.

    Args:
        job_url: URL of the Greenhouse job posting
        user_data: User data dict (if None, uses placeholders)
        include_upload: Whether to include resume upload step

    Returns:
        List of step dicts ready for WorkflowStep construction
    """
    steps = [
        {"action": "goto", "url": job_url},
        {"action": "wait", "duration": 2000},
        {"action": "click", "auto_detect": True},  # Apply button
        {"action": "wait", "duration": 2000},
        {
            "action": "fill_form",
            "fields": get_default_user_data_fields() if not user_data else user_data,
            "auto_detect": True
        },
    ]

    if include_upload:
        steps.append({
            "action": "upload",
            "selector": "input[type='file']",
            "file": "{{user.resume_path}}",
            "auto_detect": True
        })

    steps.append({"action": "screenshot"})

    return steps


# Common Greenhouse form field labels (for logging/debugging)
GREENHOUSE_FIELD_LABELS = {
    "first_name": "First Name",
    "last_name": "Last Name",
    "email": "Email",
    "phone": "Phone",
    "location": "Location / City",
    "linkedin_url": "LinkedIn Profile URL",
    "resume": "Resume / CV",
}


def get_field_label(field_name: str) -> str:
    """Get human-readable label for a field name."""
    normalized = normalize_field_name(field_name)
    return GREENHOUSE_FIELD_LABELS.get(normalized, field_name.replace("_", " ").title())
