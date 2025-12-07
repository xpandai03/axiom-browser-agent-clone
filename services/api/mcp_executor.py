"""
MCP Executor for workflow execution via Playwright MCP.

Loops through WorkflowSteps and calls MCP actions for each step,
capturing screenshots after each action.

This executor works with Cursor/Claude's native MCP integration -
no external Node processes need to be spawned.
"""

import asyncio
import logging
import time
from typing import List, Optional, Dict
from datetime import datetime

from shared.schemas.workflow import WorkflowStep
from shared.schemas.execution import StepResult, WorkflowResult
from .mcp_client import BaseMCPClient, MCPToolResult, get_mcp_client

logger = logging.getLogger(__name__)


class MCPExecutor:
    """
    Executes browser automation workflows using Playwright MCP.

    Features:
    - Real browser automation via MCP tools
    - Screenshot capture after each step (base64)
    - Error handling with detailed logging
    - User data interpolation for placeholders
    - Works with Cursor's native MCP integration
    """

    def __init__(self, client: BaseMCPClient = None, use_simulation: bool = False):
        self._client = client
        self._use_simulation = use_simulation

    async def _get_client(self) -> BaseMCPClient:
        """Get or create MCP client."""
        if self._client is None:
            self._client = await get_mcp_client(use_simulation=self._use_simulation)
        return self._client

    async def execute_workflow(
        self,
        steps: List[WorkflowStep],
        user_data: Optional[Dict[str, str]] = None,
    ) -> WorkflowResult:
        """
        Execute a complete workflow.

        Args:
            steps: List of workflow steps to execute
            user_data: User data for placeholder interpolation

        Returns:
            WorkflowResult with all step results and screenshots
        """
        result = WorkflowResult()
        client = await self._get_client()

        logger.info(f"Starting workflow execution: {len(steps)} steps")

        for i, step in enumerate(steps):
            # Interpolate user data placeholders
            if user_data:
                step = step.interpolate(user_data)

            step_result = await self._execute_step(client, step, i)
            result.add_step_result(step_result)

            # Stop if step failed critically (navigation failures are critical)
            if step_result.status == "failed" and step.action in ("goto",):
                logger.warning(f"Stopping workflow after critical failure at step {i}")
                break

        result.complete()
        logger.info(f"Workflow completed: success={result.success}, steps={len(result.steps)}")

        return result

    async def _execute_step(
        self,
        client: BaseMCPClient,
        step: WorkflowStep,
        step_number: int,
    ) -> StepResult:
        """Execute a single workflow step via MCP."""
        start_time = time.time()
        logs = []

        logger.info(f"Executing step {step_number}: {step.action}")
        logs.append(f"Executing {step.action}")

        try:
            # Execute action based on type
            action_result = await self._execute_action(client, step)

            if action_result.success:
                logs.append(f"Action completed: {action_result.content or 'OK'}")
            else:
                logs.append(f"Action failed: {action_result.error}")

            # Capture screenshot after step
            screenshot_base64 = await self._capture_screenshot(client)

            duration_ms = int((time.time() - start_time) * 1000)

            return StepResult(
                step_number=step_number,
                action=step.action,
                status="success" if action_result.success else "failed",
                duration_ms=duration_ms,
                screenshot_base64=screenshot_base64,
                logs=logs,
                error=action_result.error,
                timestamp=datetime.utcnow(),
            )

        except Exception as e:
            logger.error(f"Step {step_number} failed with exception: {e}")
            logs.append(f"Exception: {str(e)}")

            # Try to capture error screenshot
            screenshot_base64 = await self._capture_screenshot(client)

            duration_ms = int((time.time() - start_time) * 1000)

            return StepResult(
                step_number=step_number,
                action=step.action,
                status="failed",
                duration_ms=duration_ms,
                screenshot_base64=screenshot_base64,
                logs=logs,
                error=str(e),
                timestamp=datetime.utcnow(),
            )

    async def _execute_action(
        self,
        client: BaseMCPClient,
        step: WorkflowStep,
    ) -> MCPToolResult:
        """Execute the appropriate MCP action for a workflow step."""

        if step.action == "goto":
            if not step.url:
                return MCPToolResult(success=False, content=None, error="No URL provided")
            return await client.navigate(step.url)

        elif step.action == "click":
            if not step.selector:
                return MCPToolResult(success=False, content=None, error="No selector provided")
            return await client.click(step.selector)

        elif step.action == "type":
            if not step.selector:
                return MCPToolResult(success=False, content=None, error="No selector provided")
            value = step.value or ""
            return await client.fill(step.selector, value)

        elif step.action == "upload":
            if not step.selector:
                return MCPToolResult(success=False, content=None, error="No selector provided")
            if not step.file:
                return MCPToolResult(success=False, content=None, error="No file provided")
            return await client.select_file(step.selector, [step.file])

        elif step.action == "wait":
            duration = step.duration or 1000
            # Simple sleep for wait action
            await asyncio.sleep(duration / 1000.0)
            return MCPToolResult(success=True, content=f"Waited {duration}ms")

        elif step.action == "scroll":
            if step.selector:
                # Wait for element then scroll to it
                await client.wait_for_selector(step.selector)
            return await client.scroll("down")

        else:
            return MCPToolResult(
                success=False,
                content=None,
                error=f"Unknown action: {step.action}"
            )

    async def _capture_screenshot(self, client: BaseMCPClient) -> Optional[str]:
        """Capture a screenshot and return as base64 string."""
        try:
            result = await client.screenshot()

            if result.success:
                # Check for screenshot in the result
                if result.screenshot_base64:
                    return result.screenshot_base64
                # Some MCP responses include screenshot in content
                if isinstance(result.content, dict) and "screenshot_base64" in result.content:
                    return result.content["screenshot_base64"]

            logger.warning("Screenshot capture returned no data")
            return None

        except Exception as e:
            logger.error(f"Failed to capture screenshot: {e}")
            return None


async def execute_workflow(
    steps: List[WorkflowStep],
    user_data: Optional[Dict[str, str]] = None,
    use_simulation: bool = False,
) -> WorkflowResult:
    """
    Convenience function to execute workflow steps via MCP.

    Args:
        steps: List of workflow steps
        user_data: User data for placeholder interpolation
        use_simulation: Use simulated MCP client for testing

    Returns:
        WorkflowResult with execution results
    """
    executor = MCPExecutor(use_simulation=use_simulation)
    return await executor.execute_workflow(steps, user_data)
