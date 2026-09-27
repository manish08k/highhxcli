"""Agent tools: HighhX capabilities the AI can use, each gated by risk, policy and approvals."""

from highhx.agent.tools.base import Tool, ToolContext, ToolError, ToolResult
from highhx.agent.tools.registry import ToolRegistry, builtin_tools

__all__ = ["Tool", "ToolContext", "ToolError", "ToolRegistry", "ToolResult", "builtin_tools"]
