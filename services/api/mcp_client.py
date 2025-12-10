"""
MCP Client for Playwright browser automation.

This client is designed to work with Cursor/Claude's native MCP integration.
The Playwright MCP server is managed by the IDE - we simply call the tools
that are exposed through the MCP protocol.

When running in Cursor with the Playwright MCP server configured, the tools
are available as: browser_navigate, browser_click, browser_fill, etc.

For standalone usage, this module provides a mock/simulation mode.
"""

import logging
from typing import Any, Dict, Optional, List, Union
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


@dataclass
class MCPToolResult:
    """Result from an MCP tool call."""
    success: bool
    content: Any = None
    error: Optional[str] = None
    screenshot_base64: Optional[str] = None
    extracted_data: Optional[Union[List[str], str]] = None


class BaseMCPClient(ABC):
    """Abstract base class for MCP clients."""

    @abstractmethod
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
        """Call an MCP tool."""
        pass

    # Convenience methods for Playwright MCP tools

    async def navigate(self, url: str) -> MCPToolResult:
        """Navigate to a URL."""
        return await self.call_tool("browser_navigate", {"url": url})

    async def click(self, selector: str) -> MCPToolResult:
        """Click an element."""
        return await self.call_tool("browser_click", {"selector": selector})

    async def fill(self, selector: str, value: str) -> MCPToolResult:
        """Fill text into an input field."""
        return await self.call_tool("browser_fill", {"selector": selector, "value": value})

    async def type_text(self, selector: str, text: str) -> MCPToolResult:
        """Type text into an element (keystroke by keystroke)."""
        return await self.call_tool("browser_type", {"selector": selector, "text": text})

    async def screenshot(self) -> MCPToolResult:
        """Take a screenshot of the current page."""
        return await self.call_tool("browser_screenshot", {})

    async def wait_for_selector(self, selector: str, timeout: int = 30000) -> MCPToolResult:
        """Wait for an element to appear."""
        return await self.call_tool("browser_wait_for", {"selector": selector, "timeout": timeout})

    async def scroll(self, direction: str = "down", amount: int = None) -> MCPToolResult:
        """Scroll the page."""
        args = {"direction": direction}
        if amount:
            args["amount"] = amount
        return await self.call_tool("browser_scroll", args)

    async def select_file(self, selector: str, paths: List[str]) -> MCPToolResult:
        """Select files for upload."""
        return await self.call_tool("browser_file_upload", {
            "selector": selector,
            "paths": paths,
        })

    async def get_content(self, selector: str = None) -> MCPToolResult:
        """Get page content or element content."""
        args = {"selector": selector} if selector else {}
        return await self.call_tool("browser_get_content", args)

    async def extract(self, selector: str = None, extract_mode: str = "text", attribute: str = None) -> MCPToolResult:
        """Extract text or attribute from elements."""
        args = {"extract_mode": extract_mode}
        if selector:
            args["selector"] = selector
        if attribute:
            args["attribute"] = attribute
        return await self.call_tool("browser_extract", args)

    async def close(self) -> MCPToolResult:
        """Close the browser."""
        return await self.call_tool("browser_close", {})

    async def get_current_url(self) -> MCPToolResult:
        """Get the current page URL."""
        return await self.call_tool("browser_get_current_url", {})

    async def get_element_count(self, selector: str) -> MCPToolResult:
        """Get the count of elements matching a selector."""
        return await self.call_tool("browser_get_element_count", {"selector": selector})

    async def click_first_job(self) -> MCPToolResult:
        """Click the first job listing on a Greenhouse index page."""
        return await self.call_tool("browser_click_first_job", {})

    async def scroll_to_element(self, selector: str) -> MCPToolResult:
        """Scroll to bring an element into view."""
        return await self.call_tool("browser_scroll_to_element", {"selector": selector})

    async def scroll_until_text(self, text: str, max_scrolls: int = 10) -> MCPToolResult:
        """Scroll until specific text is found on the page."""
        return await self.call_tool("browser_scroll_until_text", {"text": text, "max_scrolls": max_scrolls})


class CursorMCPClient(BaseMCPClient):
    """
    MCP Client for Cursor/Claude's native MCP integration.

    This client expects the Playwright MCP server to be configured in Cursor's
    MCP settings. The tools are called through Claude's native tool-calling
    mechanism when running inside Cursor.

    For API server usage, this wraps the MCP tool calls in a way that can be
    invoked programmatically. The actual MCP communication is handled by
    the runtime environment.
    """

    def __init__(self):
        self._initialized = False
        self._tool_results: Dict[str, MCPToolResult] = {}

    async def initialize(self) -> None:
        """Initialize the MCP client connection."""
        if not self._initialized:
            logger.info("MCP Client initialized - using Cursor's native MCP integration")
            self._initialized = True

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
        """
        Call an MCP tool through Cursor's native integration.

        In Cursor, this would invoke the tool through the MCP protocol.
        The tool results are returned asynchronously.
        """
        logger.info(f"MCP call: {tool_name}({arguments})")

        try:
            # This is where the actual MCP call happens.
            # In Cursor's runtime, this connects to the configured MCP server.
            #
            # The pattern is:
            # 1. Cursor has Playwright MCP configured in .cursor/mcp.json
            # 2. When this code runs, it calls the MCP tool
            # 3. Cursor routes the call to the Playwright MCP server
            # 4. Results come back through the MCP protocol

            # For now, we'll use a request/response pattern that works
            # with the MCP SDK when available
            result = await self._execute_mcp_tool(tool_name, arguments)
            return result

        except Exception as e:
            logger.error(f"MCP tool call failed: {tool_name} - {e}")
            return MCPToolResult(
                success=False,
                content=None,
                error=str(e)
            )

    async def _execute_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
        """
        Execute an MCP tool call.

        This method should be overridden or configured based on the runtime:
        - In Cursor: Uses native MCP integration
        - In standalone: Uses direct Playwright (fallback)
        """
        # Import the MCP tool executor based on runtime
        from .mcp_runtime import execute_mcp_tool
        return await execute_mcp_tool(tool_name, arguments)


class SimulatedMCPClient(BaseMCPClient):
    """
    Simulated MCP Client for testing and development.

    This client simulates MCP tool calls without actually performing
    browser automation. Useful for testing the workflow logic.
    """

    def __init__(self):
        self._step_count = 0

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
        """Simulate an MCP tool call."""
        self._step_count += 1
        logger.info(f"[SIMULATED] MCP call #{self._step_count}: {tool_name}({arguments})")

        # Simulate success for all calls
        if tool_name == "browser_screenshot":
            # Return a placeholder for screenshot
            return MCPToolResult(
                success=True,
                content="Screenshot captured",
                screenshot_base64=None  # No actual screenshot in simulation
            )

        return MCPToolResult(
            success=True,
            content=f"Simulated {tool_name} completed",
            error=None
        )


# Client factory
_mcp_client: Optional[BaseMCPClient] = None


async def get_mcp_client(use_simulation: bool = False) -> BaseMCPClient:
    """
    Get or create the MCP client.

    Args:
        use_simulation: If True, use simulated client for testing

    Returns:
        MCP client instance
    """
    global _mcp_client

    if _mcp_client is None:
        if use_simulation:
            _mcp_client = SimulatedMCPClient()
            logger.info("Using simulated MCP client")
        else:
            _mcp_client = CursorMCPClient()
            await _mcp_client.initialize()
            logger.info("Using Cursor MCP client")

    return _mcp_client


async def shutdown_mcp_client() -> None:
    """Shutdown the MCP client."""
    global _mcp_client
    if _mcp_client:
        try:
            await _mcp_client.close()
        except Exception as e:
            logger.warning(f"Error closing MCP client: {e}")
        _mcp_client = None
