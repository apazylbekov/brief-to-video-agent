"""Errors whose messages are written for the agent. The MCP tools pass them on as ToolError."""


class ReportableError(Exception):
    """A failure the agent should read and act on: fix the input, retry, or stop."""
