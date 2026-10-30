"""MCP server exposing the brief-to-video pipeline as tools. Run: uv run python -m app.server"""

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, SkipValidation

from app import clips, report, retrieval, review, shot_list
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


@server.tool()
def review_clip(
    run_id: Annotated[str, Field(description="run_id returned by save_shot_list")],
    shot_index: Annotated[int, Field(ge=1, description="The shot's index in that list")],
) -> review.ReviewPacket:
    """Extract the start, middle and end frames of a shot's clip, with the shot's spec.

    Look at all three frames, judge them with the QA rubric in CLAUDE.md, then call
    record_review with the verdict.
    """
    try:
        return review.frames(run_id, shot_index)
    except ReportableError as exc:
        raise ToolError(str(exc)) from exc


@server.tool()
def record_review(
    run_id: Annotated[str, Field(description="run_id returned by save_shot_list")],
    shot_index: Annotated[int, Field(ge=1, description="The shot's index in that list")],
    verdict: Literal["pass", "fail"],
    reasons: Annotated[str, Field(min_length=1, max_length=300, description="One line")],
) -> review.RecordedReview:
    """Record a QA verdict for a shot's current clip in runs/runs.csv and shots.json.

    Call review_clip first. A shot's first fail is regenerated once right away, within
    MAX_SPEND_USD (in live mode that takes minutes and costs money); review the new clip the
    same way. A second fail is final. next_step says what to do.
    """
    try:
        return review.record(run_id, shot_index, verdict, reasons)
    except ReportableError as exc:
        raise ToolError(str(exc)) from exc


@server.tool()
def build_report(
    run_id: Annotated[str, Field(description="run_id returned by save_shot_list")],
) -> report.BuiltReport:
    """Write runs/<run_id>/report.html: run totals, the brief, and one card per shot (video,
    spec, model and reason, QA verdict, attempts, cost and latency). Call it at the end."""
    try:
        return report.build(run_id)
    except ReportableError as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    server.run()
