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
from shared.schemas.execution import StepResult, WorkflowResult, FieldFillResult, JobExtractResult
from .mcp_client import BaseMCPClient, MCPToolResult, get_mcp_client
from . import greenhouse_helpers

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
        self._workflow_context: Dict[str, any] = {}  # Context for passing data between steps

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

            # Check if loop_jobs returned jobs data
            if step.action == "loop_jobs" and hasattr(self, '_last_jobs_data'):
                for job in self._last_jobs_data:
                    result.add_job_result(job)
                delattr(self, '_last_jobs_data')

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
        extracted_data = None
        fields_filled = None

        logger.info(f"Executing step {step_number}: {step.action}")
        logs.append(f"Executing {step.action}")

        try:
            # Execute action based on type
            action_result = await self._execute_action(client, step)

            if action_result.success:
                logs.append(f"Action completed: {action_result.content or 'OK'}")
            else:
                logs.append(f"Action failed: {action_result.error}")

            # Capture extracted data if present
            if action_result.extracted_data is not None:
                extracted_data = action_result.extracted_data
                label = getattr(step, 'label', None) or step.selector or 'data'
                # Store in workflow context for use by subsequent steps
                self._workflow_context[label] = extracted_data
                if isinstance(extracted_data, list):
                    logs.append(f"Extracted [{label}]: {len(extracted_data)} items")
                else:
                    preview = str(extracted_data)[:100]
                    if len(str(extracted_data)) > 100:
                        preview += "..."
                    logs.append(f"Extracted [{label}]: {preview}")

            # Capture fields_filled data if present (from fill_form action)
            if action_result.fields_filled:
                fields_filled = action_result.fields_filled

            # Capture jobs data if present (from loop_jobs action)
            if action_result.jobs_data:
                self._last_jobs_data = action_result.jobs_data
                logger.info(f"Captured {len(action_result.jobs_data)} job results for WorkflowResult")

            # Capture screenshot after step (unless it's a screenshot action itself)
            if step.action == "screenshot":
                screenshot_base64 = action_result.screenshot_base64
            else:
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
                extracted_data=extracted_data,
                fields_filled=fields_filled,
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
                extracted_data=None,
                fields_filled=None,
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
            # Click supports auto-detection when auto_detect=True
            if step.auto_detect and not step.selector:
                # Let the runtime handle auto-detection with its Greenhouse-aware selectors
                return await client.click(None)
            return await client.click(step.selector)

        elif step.action == "type":
            if not step.selector:
                return MCPToolResult(success=False, content=None, error="No selector provided")
            value = step.value or ""
            return await client.fill(step.selector, value)

        elif step.action == "upload":
            if not step.selector and not step.auto_detect:
                return MCPToolResult(success=False, content=None, error="No selector provided")
            if not step.file:
                # Stub upload - log and continue
                logger.info("Upload step with no file - skipping (upload_skipped)")
                return MCPToolResult(
                    success=True,
                    content="upload_skipped: No file provided"
                )
            selector = step.selector or "input[type='file']"
            return await client.select_file(selector, [step.file])

        elif step.action == "wait":
            duration = step.duration or 1000
            # Simple sleep for wait action
            await asyncio.sleep(duration / 1000.0)
            return MCPToolResult(success=True, content=f"Waited {duration}ms")

        elif step.action == "scroll":
            scroll_mode = getattr(step, 'scroll_mode', 'pixels')

            if scroll_mode == 'to_element':
                if not step.selector:
                    return MCPToolResult(success=False, content=None, error="No selector provided for scroll-to-element")
                return await client.scroll_to_element(step.selector)

            elif scroll_mode == 'until_text':
                scroll_text = getattr(step, 'scroll_text', None)
                if not scroll_text:
                    return MCPToolResult(success=False, content=None, error="No text provided for scroll-until-text")
                return await client.scroll_until_text(scroll_text)

            else:  # pixels mode (default)
                direction = getattr(step, 'scroll_direction', 'down')
                amount = getattr(step, 'scroll_amount', None)
                return await client.scroll(direction, amount)

        elif step.action == "extract":
            # Extract text or attributes from page elements
            return await client.extract(
                selector=step.selector,
                extract_mode=step.extract_mode,
                attribute=step.attribute
            )

        elif step.action == "screenshot":
            # Take a screenshot explicitly
            return await client.screenshot()

        elif step.action == "fill_form":
            # Fill multiple form fields
            return await self._execute_fill_form(client, step)

        elif step.action == "click_first_job":
            # Click first job listing on a Greenhouse index page
            return await client.click_first_job()

        elif step.action == "extract_job_links":
            # Extract all job links from a Greenhouse board page
            return await client.extract_job_links()

        elif step.action == "loop_jobs":
            # Loop through extracted job URLs and scrape each
            return await self._execute_loop_jobs(client, step)

        else:
            return MCPToolResult(
                success=False,
                content=None,
                error=f"Unknown action: {step.action}"
            )

    async def _execute_fill_form(
        self,
        client: BaseMCPClient,
        step: WorkflowStep,
    ) -> MCPToolResult:
        """
        Fill multiple form fields in one step.

        Uses greenhouse_helpers for auto-detection of field selectors.
        """
        if not step.fields:
            return MCPToolResult(
                success=False,
                content=None,
                error="No fields provided for fill_form action"
            )

        fields_filled: List[FieldFillResult] = []
        errors = []
        success_count = 0

        logger.info(f"Filling form with {len(step.fields)} fields")

        for field_name, value in step.fields.items():
            # Skip empty values
            if not value or value.startswith("{{user."):
                logger.warning(f"Skipping field {field_name}: empty or uninterpolated placeholder")
                fields_filled.append(FieldFillResult(
                    field_name=field_name,
                    selector_used="none",
                    success=False,
                    error="Empty value or uninterpolated placeholder"
                ))
                continue

            # Get selectors for this field
            if step.auto_detect:
                selectors = greenhouse_helpers.get_field_selectors(field_name)
            elif step.selector:
                selectors = [step.selector]
            else:
                selectors = greenhouse_helpers.get_field_selectors(field_name)

            # Try to fill the field
            success, selector_used, error = await greenhouse_helpers.try_selectors(
                client, selectors, action="fill", value=value
            )

            field_label = greenhouse_helpers.get_field_label(field_name)

            if success:
                success_count += 1
                logger.info(f"Filled {field_label} using selector: {selector_used}")
                fields_filled.append(FieldFillResult(
                    field_name=field_name,
                    selector_used=selector_used,
                    success=True
                ))
            else:
                logger.warning(f"Could not fill {field_label}: {error}")
                errors.append(f"{field_label}: {error}")
                fields_filled.append(FieldFillResult(
                    field_name=field_name,
                    selector_used="none",
                    success=False,
                    error=error
                ))

        # Determine overall success (at least one field filled)
        overall_success = success_count > 0
        status = "success" if success_count == len(step.fields) else "partial" if success_count > 0 else "failed"

        content = f"Filled {success_count}/{len(step.fields)} fields"
        if errors:
            content += f". Errors: {'; '.join(errors[:3])}"  # Limit error list

        # Create result with fields_filled attached
        return MCPToolResult(
            success=overall_success,
            content=content,
            error="; ".join(errors) if errors and not overall_success else None,
            fields_filled=fields_filled,  # Pass fields_filled through the proper field
        )

    async def _execute_loop_jobs(
        self,
        client: BaseMCPClient,
        step: WorkflowStep,
    ) -> MCPToolResult:
        """
        Loop through extracted job URLs and scrape each one.

        Uses workflow context to get job URLs from previous extract_job_links step.
        Extracts title, location, description, and screenshot for each job.
        """
        # Get job URLs from workflow context
        source_label = step.job_url_source or "job_links"
        job_urls = self._workflow_context.get(source_label, [])

        if not job_urls:
            return MCPToolResult(
                success=False,
                content=None,
                error=f"No job URLs found in context under '{source_label}'. Run extract_job_links first."
            )

        max_jobs = step.max_jobs or 5
        job_urls = job_urls[:max_jobs]

        logger.info(f"Processing {len(job_urls)} jobs (max: {max_jobs})")

        jobs_processed: List[JobExtractResult] = []
        errors = []

        for i, url in enumerate(job_urls):
            job_result = JobExtractResult(job_index=i, url=url)

            try:
                logger.info(f"Processing job {i+1}/{len(job_urls)}: {url}")

                # Navigate to job page
                nav_result = await client.navigate(url)
                if not nav_result.success:
                    job_result.success = False
                    job_result.error = f"Navigation failed: {nav_result.error}"
                    jobs_processed.append(job_result)
                    continue

                # Wait for page load
                await asyncio.sleep(1.5)

                # Check if we landed on index page (need to click first job)
                current_url_result = await client.get_current_url()
                current_url = current_url_result.content if current_url_result.success else ""
                if current_url and "/jobs" not in current_url:
                    # We're on a board page, try to click first job
                    click_result = await client.click_first_job()
                    if click_result.success:
                        await asyncio.sleep(1.5)

                # Extract job title
                title_selectors = ["h1", ".job-title", "[data-test='job-title']", ".app-title"]
                for sel in title_selectors:
                    try:
                        title_result = await client.extract(sel, "text")
                        if title_result.success and title_result.extracted_data:
                            data = title_result.extracted_data
                            job_result.title = data[0] if isinstance(data, list) else data
                            break
                    except Exception:
                        continue

                # Extract location
                location_selectors = [
                    "[class*='location']",
                    ".app-title + p",
                    "h1 + div",
                    ".job-location",
                    ".location",
                ]
                for sel in location_selectors:
                    try:
                        loc_result = await client.extract(sel, "text")
                        if loc_result.success and loc_result.extracted_data:
                            data = loc_result.extracted_data
                            job_result.location = data[0] if isinstance(data, list) else data
                            break
                    except Exception:
                        continue

                # Extract description (first 2000 chars)
                desc_selectors = [
                    "#main",
                    "main",
                    "article",
                    ".job-description",
                    "#content",
                    ".content",
                ]
                for sel in desc_selectors:
                    try:
                        desc_result = await client.extract(sel, "text")
                        if desc_result.success and desc_result.extracted_data:
                            data = desc_result.extracted_data
                            desc = data[0] if isinstance(data, list) else data
                            job_result.description = desc[:2000] if desc else None
                            break
                    except Exception:
                        continue

                # Take screenshot
                screenshot_result = await client.screenshot()
                if screenshot_result.success:
                    job_result.screenshot_base64 = screenshot_result.screenshot_base64

                job_result.success = True
                logger.info(f"Job {i+1} extracted: {job_result.title or 'No title'}")

            except Exception as e:
                logger.error(f"Error processing job {i+1}: {e}")
                job_result.success = False
                job_result.error = str(e)
                errors.append(f"Job {i+1}: {str(e)}")

            jobs_processed.append(job_result)

        # Build result
        successful = sum(1 for j in jobs_processed if j.success)

        result = MCPToolResult(
            success=successful > 0,
            content=f"Processed {successful}/{len(job_urls)} jobs successfully",
            error="; ".join(errors) if errors and successful == 0 else None,
            jobs_data=jobs_processed,  # Pass jobs data through the proper field
        )

        return result

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
