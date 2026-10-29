import asyncio
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from app.server import search_briefs, server


def test_search_briefs_direct_call() -> None:
    result = search_briefs("coffee shop in Almaty", k=2)
    assert result.method == "keyword"
    assert len(result.matches) == 2
    assert result.matches[0].id == "almaty-01"


def test_search_briefs_through_mcp() -> None:
    result = asyncio.run(server.call_tool("search_briefs", {"query": "ski rental"}))
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["method"] == "keyword"
    assert len(data["matches"]) == 3  # default k
    assert data["matches"][0]["id"] == "almaty-03"
    assert {"brand", "key_message", "must_include", "score"} <= data["matches"][0].keys()


@pytest.mark.parametrize("args", [{"query": "coffee", "k": 0}, {"query": ""}])
def test_search_briefs_rejects_invalid_arguments(args: dict[str, Any]) -> None:
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("search_briefs", args))
