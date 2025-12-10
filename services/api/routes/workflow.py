import json
import logging
import time
import asyncio
from typing import Optional, Dict, List, AsyncGenerator
from fastapi import APIRouter, HTTPException, Form, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from datetime import datetime

from shared.schemas.workflow import WorkflowStep
from shared.schemas.execution import WorkflowResult, StepResult
from shared.ai import parse_instructions_to_steps
from ..mcp_executor import MCPExecutor, execute_workflow
from ..mcp_client import get_mcp_client

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


@router.get("/run-stream")
async def run_workflow_stream(
    instructions: str = Query(..., description="Natural language workflow instructions"),
    user_data: str = Query("{}", description="JSON object with user data for placeholders"),
):
    """
    Execute a workflow with Server-Sent Events (SSE) streaming.

    Events sent:
    - workflow_parsed: When instructions are parsed into steps
    - step_start: When a step begins execution
    - step_complete: When a step finishes (with logs, screenshot, status)
    - workflow_complete: When entire workflow is done
    - error: On any error

    This allows the frontend to show real-time progress.
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        workflow_id = f"wf_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        start_time = time.time()
        all_steps = []

        try:
            # Parse user data
            try:
                user_data_dict = json.loads(user_data) if user_data else {}
            except json.JSONDecodeError as e:
                yield f"event: error\ndata: {json.dumps({'error': f'Invalid user_data JSON: {e}'})}\n\n"
                return

            # Parse instructions to workflow steps
            logger.info(f"[Stream] Parsing instructions: {instructions[:100]}...")
            yield f"event: status\ndata: {json.dumps({'message': 'Parsing workflow instructions...'})}\n\n"

            steps = await parse_instructions_to_steps(instructions)

            if not steps:
                yield f"event: error\ndata: {json.dumps({'error': 'Failed to parse workflow instructions'})}\n\n"
                return

            # Send parsed workflow
            workflow_steps_data = [step.model_dump() for step in steps]
            yield f"event: workflow_parsed\ndata: {json.dumps({'workflow_id': workflow_id, 'steps': workflow_steps_data, 'count': len(steps)})}\n\n"

            logger.info(f"[Stream] Parsed {len(steps)} workflow steps")

            # Get MCP client and create executor
            client = await get_mcp_client()
            executor = MCPExecutor(client=client)

            # Execute each step and stream results
            for i, step in enumerate(steps):
                # Interpolate user data
                if user_data_dict:
                    step = step.interpolate(user_data_dict)

                # Send step_start event
                yield f"event: step_start\ndata: {json.dumps({'step_number': i, 'action': step.action, 'total_steps': len(steps)})}\n\n"

                # Execute the step
                step_result = await executor._execute_step(client, step, i)
                all_steps.append(step_result)

                # Send step_complete event with full result
                step_data = step_result.model_dump()
                # Ensure timestamp is serialized properly
                if 'timestamp' in step_data and step_data['timestamp']:
                    step_data['timestamp'] = step_data['timestamp'].isoformat()

                yield f"event: step_complete\ndata: {json.dumps(step_data)}\n\n"

                logger.info(f"[Stream] Step {i} completed: {step_result.status}")

                # Stop if critical failure
                if step_result.status == "failed" and step.action in ("goto",):
                    logger.warning(f"[Stream] Stopping after critical failure at step {i}")
                    break

            # Send workflow_complete event
            total_duration = int((time.time() - start_time) * 1000)
            success = all(s.status == "success" for s in all_steps)

            complete_data = {
                'workflow_id': workflow_id,
                'success': success,
                'total_duration_ms': total_duration,
                'steps_completed': len(all_steps),
                'steps_total': len(steps),
            }
            yield f"event: workflow_complete\ndata: {json.dumps(complete_data)}\n\n"

            logger.info(f"[Stream] Workflow completed: success={success}, duration={total_duration}ms")

        except Exception as e:
            logger.error(f"[Stream] Workflow failed: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )
