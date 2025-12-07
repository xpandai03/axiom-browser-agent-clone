import json
import logging
from typing import Optional, Dict, List
from fastapi import APIRouter, HTTPException, Form, UploadFile, File
from pydantic import BaseModel

from shared.schemas.workflow import WorkflowStep
from shared.schemas.execution import WorkflowResult
from shared.ai import parse_instructions_to_steps
from ..mcp_executor import execute_workflow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/workflow", tags=["workflow"])


class WorkflowRunRequest(BaseModel):
    """Request body for running a workflow."""
    instructions: str
    user_data: Optional[Dict[str, str]] = None


class StepResponse(BaseModel):
    """Response for a single step execution."""
    step_number: int
    action: str
    status: str
    duration_ms: int
    logs: List[str]
    error: Optional[str] = None
    screenshot_base64: Optional[str] = None


class WorkflowResponse(BaseModel):
    """Response containing workflow execution results."""
    workflow_id: str
    success: bool
    workflow_steps: List[dict]  # Parsed steps from AI
    steps: List[dict]  # Execution results
    total_duration_ms: int
    error: Optional[str] = None
    tailored_resume: Optional[str] = None


@router.post("/run")
async def run_workflow(request: WorkflowRunRequest):
    """
    Execute a workflow from natural language instructions (JSON body).
    """
    try:
        # Parse instructions to workflow steps
        logger.info(f"Parsing instructions: {request.instructions[:100]}...")
        steps = await parse_instructions_to_steps(request.instructions)

        if not steps:
            raise HTTPException(status_code=400, detail="Failed to parse workflow instructions")

        logger.info(f"Parsed {len(steps)} workflow steps")

        # Execute workflow via MCP
        logger.info("Executing workflow via MCP...")
        result = await execute_workflow(
            steps=steps,
            user_data=request.user_data,
        )

        # Build response
        return {
            "workflow_id": result.workflow_id,
            "success": result.success,
            "workflow_steps": [step.model_dump() for step in steps],
            "steps": [step.model_dump() for step in result.steps],
            "total_duration_ms": result.total_duration_ms,
            "error": result.error,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Workflow execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run-sync")
async def run_workflow_sync(
    instructions: str = Form(..., description="Natural language workflow instructions"),
    job_description: str = Form("", description="Job description for resume tailoring"),
    resume: Optional[UploadFile] = File(None, description="Resume file"),
    user_data: str = Form("{}", description="JSON object with user data for placeholders"),
):
    """
    Execute a workflow and return results (form data - for frontend).

    This is the main endpoint for the web UI.
    Returns:
    - workflow_id: Unique ID for this execution
    - success: Whether all steps completed successfully
    - workflow_steps: The parsed workflow steps (from AI)
    - steps: Execution results with logs and screenshots
    - total_duration_ms: Total execution time
    - error: Top-level error message if any
    - tailored_resume: Tailored resume content if requested
    """
    try:
        # Parse user data
        try:
            user_data_dict = json.loads(user_data) if user_data else {}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid user_data JSON: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid user_data JSON: {e}")

        # Read resume if provided
        resume_text = None
        if resume:
            try:
                content = await resume.read()
                resume_text = content.decode("utf-8", errors="ignore")
                logger.info(f"Read resume: {len(resume_text)} characters")
            except Exception as e:
                logger.error(f"Failed to read resume file: {e}")

        # Parse instructions to workflow steps
        logger.info(f"Parsing instructions: {instructions[:100]}...")
        steps = await parse_instructions_to_steps(instructions)

        if not steps:
            raise HTTPException(status_code=400, detail="Failed to parse workflow instructions")

        logger.info(f"Parsed {len(steps)} workflow steps: {[s.action for s in steps]}")

        # Execute workflow via MCP
        logger.info("Executing workflow via MCP...")
        result = await execute_workflow(
            steps=steps,
            user_data=user_data_dict,
        )

        logger.info(f"Workflow completed: success={result.success}, steps={len(result.steps)}")

        # Build response matching frontend expectations
        response = {
            "workflow_id": result.workflow_id,
            "success": result.success,
            "workflow_steps": [step.model_dump() for step in steps],
            "steps": [step.model_dump() for step in result.steps],
            "total_duration_ms": result.total_duration_ms,
            "error": result.error,
        }

        # Include tailored resume if requested
        if resume_text and job_description:
            logger.info("Generating tailored resume...")
            from shared.ai import generate_tailored_resume
            tailored = await generate_tailored_resume(resume_text, job_description)
            response["tailored_resume"] = tailored.content if tailored.success else None

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Workflow execution failed: {e}", exc_info=True)
        return {
            "workflow_id": "error",
            "success": False,
            "workflow_steps": [],
            "steps": [],
            "total_duration_ms": 0,
            "error": str(e),
        }


@router.post("/parse")
async def parse_workflow(instructions: str = Form(...)):
    """
    Parse natural language instructions into workflow steps.

    Useful for previewing what steps will be executed without running them.
    """
    try:
        steps = await parse_instructions_to_steps(instructions)

        return {
            "success": True,
            "steps": [step.model_dump() for step in steps],
            "count": len(steps),
        }
    except Exception as e:
        logger.error(f"Parse failed: {e}")
        return {
            "success": False,
            "steps": [],
            "count": 0,
            "error": str(e),
        }


@router.post("/execute-steps")
async def execute_steps(
    steps: List[dict],
    user_data: Optional[Dict[str, str]] = None,
):
    """
    Execute pre-parsed workflow steps directly.

    Useful when steps have already been parsed or modified.
    """
    try:
        # Convert to WorkflowStep objects
        workflow_steps = [WorkflowStep(**step) for step in steps]

        # Execute workflow via MCP
        result = await execute_workflow(
            steps=workflow_steps,
            user_data=user_data,
        )

        return {
            "workflow_id": result.workflow_id,
            "success": result.success,
            "steps": [step.model_dump() for step in result.steps],
            "total_duration_ms": result.total_duration_ms,
            "error": result.error,
        }
    except Exception as e:
        logger.error(f"Execute steps failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
