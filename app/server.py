"""MCP server exposing the brief-to-video pipeline as tools. Run: uv run python -m app.server"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from app import retrieval

server = MCPServer("brief-to-video-agent")


@server.tool()
def search_briefs(
    query: Annotated[str, Field(min_length=1, description="One-line creative request")],
    k: Annotated[int, Field(ge=1, description="Number of briefs to return")] = 3,
) -> retrieval.SearchResult:
    """Find the creative briefs that best match a request, best match first.

    `method` says how matches were scored: "embedding" (cosine similarity, -1 to 1) or
    "keyword" (share of the request's keywords found in the brief, 0 to 1).
    """
    return retrieval.search(query, k)


if __name__ == "__main__":
    server.run()
