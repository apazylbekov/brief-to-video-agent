import asyncio
import csv
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from app import clips, config, run_log, shot_list
from app.server import server
from tests.factories import make_shots


def _call(tool: str, arguments: dict[str, object]) -> CallToolResult:
    """Call a tool the way an agent does: over MCP, through an in-memory client."""

    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(call())


def _ok(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def _error_text(result: CallToolResult) -> str:
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def _review(run_id: str, shot: int, verdict: str, reasons: str) -> dict[str, Any]:
    _ok(_call("review_clip", {"run_id": run_id, "shot_index": shot}))
    args = {"run_id": run_id, "shot_index": shot, "verdict": verdict, "reasons": reasons}
    return _ok(_call("record_review", args))


def _generations(run_id: str, shot: int) -> list[str]:
    return [r["attempt"] for r in run_log.generations(run_id, shot)]


@pytest.fixture
def run_id() -> str:
    """A saved run of three 5 s draft shots, with shot 1 generated (mock)."""
    run_id = shot_list.save("almaty-01", make_shots()).run_id
    clips.generate(run_id, 1)
    return run_id


# --- review_clip ----------------------------------------------------------------------------


def test_review_clip_extracts_start_middle_and_end_frames_from_a_mock_clip(run_id: str) -> None:
    packet = _ok(_call("review_clip", {"run_id": run_id, "shot_index": 1}))

    frames_dir = config.RUNS_DIR / run_id / "frames"
    assert [(f["position"], f["time_s"]) for f in packet["frames"]] == [
        ("start", 0.0),
        ("middle", 2.5),
        ("end", 5.0),
    ]
    paths = [f["path"] for f in packet["frames"]]
    assert paths == [str(frames_dir / f"shot_1_{p}.jpg") for p in ("start", "middle", "end")]
    for path in paths:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        assert probe.stdout.strip() == "mjpeg,720,1280"
    contents = [Path(path).read_bytes() for path in paths]
    assert len(set(contents)) == 3  # the test pattern moves, so the three frames differ
    assert (packet["attempt"], packet["mode"]) == (1, "mock")
    assert packet["spec"] == shot_list.load(run_id).shots[0].model_dump()


def test_review_clip_needs_a_clip_first(run_id: str) -> None:
    text = _error_text(_call("review_clip", {"run_id": run_id, "shot_index": 2}))
    assert f"shot 2 of run {run_id} has no clip yet; call generate_clip first" in text


# --- record_review --------------------------------------------------------------------------


def test_pass_is_written_to_runs_csv_and_shots_json(run_id: str) -> None:
    result = _review(run_id, 1, "pass", "subject, setting, light and dolly-in all match")

    assert (result["verdict"], result["regeneration"], result["new_clip"]) == (
        "pass",
        "not_needed",
        None,
    )
    assert result["next_step"] == "Recorded: shot 1 passed."
    row = run_log.rows()[-1]
    assert {k: row[k] for k in ("event", "shot", "attempt", "model", "cost_usd", "verdict")} == {
        "event": "review",
        "shot": "1",
        "attempt": "1",
        "model": "kling-3.0-standard",
        "cost_usd": "0.000",
        "verdict": "pass",
    }
    assert row["reasons"] == "subject, setting, light and dolly-in all match"
    [stored] = shot_list.load(run_id).reviews
    assert (stored.shot, stored.attempt, stored.verdict, stored.regeneration) == (
        1,
        1,
        "pass",
        "not_needed",
    )
    assert _generations(run_id, 1) == ["1"]


def test_first_fail_regenerates_once_and_second_fail_is_final(run_id: str) -> None:
    first = _review(run_id, 1, "fail", "warped hands in the middle frame")

    assert first["regeneration"] == "regenerated"
    assert first["new_clip"]["attempt"] == 2  # review rows don't count as attempts
    assert "Call review_clip on it" in first["next_step"]
    assert _generations(run_id, 1) == ["1", "2"]

    # The old frames are now older than the new clip, so they can't be reused for a verdict.
    args = {"run_id": run_id, "shot_index": 1, "verdict": "fail", "reasons": "same"}
    assert "review_clip hasn't been run" in _error_text(_call("record_review", args))

    second = _review(run_id, 1, "fail", "still no snow on the peaks")

    assert second["regeneration"] == "already_used"
    assert second["attempt"] == 2
    assert "stays failed" in second["next_step"]
    assert _generations(run_id, 1) == ["1", "2"]  # no third generation
    stored = [(r.attempt, r.verdict, r.regeneration) for r in shot_list.load(run_id).reviews]
    assert stored == [(1, "fail", "regenerated"), (2, "fail", "already_used")]


def test_fail_is_not_regenerated_when_the_spend_cap_would_be_passed(run_id: str) -> None:
    run_log.append(
        run_log.Row("earlier-run", 1, 1, "kling-3.0-pro", "live", 8, 90.0, Decimal("7.9"))
    )

    result = _review(run_id, 1, "fail", "subject missing in the end frame")

    assert result["regeneration"] == "blocked_by_spend_cap"
    assert "spend cap blocks its regeneration" in result["next_step"]
    assert "over MAX_SPEND_USD=$8.00" in result["next_step"]
    assert _generations(run_id, 1) == ["1"]
    assert shot_list.load(run_id).reviews[0].regeneration == "blocked_by_spend_cap"


def test_record_review_needs_review_clip_first(run_id: str) -> None:
    args = {"run_id": run_id, "shot_index": 1, "verdict": "pass", "reasons": "looks fine"}
    text = _error_text(_call("record_review", args))
    assert "review_clip hasn't been run on shot 1's current clip" in text
    assert [r["event"] for r in run_log.rows()] == ["generation"]


def test_reasons_must_be_one_line(run_id: str) -> None:
    _ok(_call("review_clip", {"run_id": run_id, "shot_index": 1}))
    args = {"run_id": run_id, "shot_index": 1, "verdict": "fail", "reasons": "hands\nface"}
    assert "reasons must be one non-empty line" in _error_text(_call("record_review", args))


def test_a_log_from_before_reviews_is_upgraded_in_place() -> None:
    config.RUNS_DIR.mkdir()
    old = [
        ["timestamp", "run_id", "shot", "attempt", "model", "mode", "seconds", "latency_s",
         "cost_usd"],
        ["2026-10-30T02:48:17+00:00", "r1", "1", "1", "kling-3.0-standard", "live", "5", "85.7",
         "0.357"],
    ]  # fmt: skip
    with run_log.path().open("w", newline="") as file:
        csv.writer(file).writerows(old)

    [row] = run_log.rows()

    assert (row["cost_usd"], row["event"], row["verdict"], row["reasons"]) == (
        "0.357",
        "generation",
        "",
        "",
    )
    assert run_log.path().read_text().splitlines()[0] == ",".join(run_log.FIELDS)
    assert run_log.total_spend() == Decimal("0.357")
    assert run_log.next_attempt("r1", 1) == 2
