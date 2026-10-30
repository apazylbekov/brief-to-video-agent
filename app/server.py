"""MCP server exposing the brief-to-video pipeline as tools. Run: uv run python -m app.server"""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, SkipValidation

from app import clips, retrieval, shot_list
from app.errors import ReportableError

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


@server.tool()
def save_shot_list(
    brief_id: Annotated[str, Field(description="id of the brief, from search_briefs")],
    # SkipValidation: the schema still shows the agent every Shot rule, but validation happens
    # in shot_list.save, which reports all problems in one readable list instead of
    # pydantic's raw dump. Field(min_length=...) would still validate, so the item limits are
    # schema-only.
    shots: Annotated[
        SkipValidation[list[shot_list.Shot]],
        Field(
            description="3-4 shots, 20 s in total at most",
            json_schema_extra={"minItems": 3, "maxItems": 4},
        ),
    ],
) -> shot_list.SavedShotList:
    """Validate a 3-4 shot list for a brief, save it as runs/<run_id>/shots.json, and price it.

    Returns the run_id and the estimated cost per shot and in total. If anything is invalid,
    nothing is saved and the error lists every problem to fix before calling again.
    """
    try:
        return shot_list.save(brief_id, shots)
    except ReportableError as exc:
        # Only ToolError text reaches the agent; other exceptions are hidden as crashes.
        raise ToolError(str(exc)) from exc


@server.tool()
def generate_clip(
    run_id: Annotated[str, Field(description="run_id returned by save_shot_list")],
    shot_index: Annotated[int, Field(ge=1, description="The shot's index in that list")],
) -> clips.GeneratedClip:
    """Generate one shot's clip and save it as runs/<run_id>/shot_<index>.mp4.

    Checks the clip's estimate first and refuses if total spend logged in runs/runs.csv would
    pass MAX_SPEND_USD. Live mode calls Higgsfield (often several minutes); mock mode writes a
    free placeholder. Every attempt is logged; failed or moderated generations cost $0.
    """
    try:
        return clips.generate(run_id, shot_index)
    except ReportableError as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    server.run()
