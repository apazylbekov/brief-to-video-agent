import asyncio
from typing import Any

from mcp import Client
from mcp.types import CallToolResult, TextContent

from app import clips, config, review, shot_list
from app.server import server
from tests.factories import make_shots


def _call(tool: str, arguments: dict[str, object]) -> CallToolResult:
    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(call())


def _ok(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def test_report_has_totals_the_brief_and_one_card_per_shot() -> None:
    shots = make_shots()
    shots[1] |= {"model": "kling-3.0-pro", "model_reason": "Hero shot <needs> detail & polish"}
    run_id = shot_list.save("almaty-01", shots).run_id
    # Shot 1 passes. Shot 2 fails, is regenerated, and its new clip isn't reviewed yet.
    # Shot 3 is never generated.
    for index in (1, 2):
        clips.generate(run_id, index)
        review.frames(run_id, index)
    review.record(run_id, 1, "pass", "mock placeholder")
    review.record(run_id, 2, "fail", "<b>warped</b> hands & a melted sign")

    built = _ok(_call("build_report", {"run_id": run_id}))

    path = config.RUNS_DIR / run_id / "report.html"
    assert built == {
        "run_id": run_id,
        "report_path": str(path),
        "shots": 3,
        "passed": 1,
        "failed": 0,
        "not_reviewed": 2,  # shot 2's current clip (attempt 2) and shot 3
        "generations": 3,
        "total_cost_usd": 0.0,
    }
    page = path.read_text()
    assert "<title>Aport &amp; Ash Roastery: Specialty coffee shop" in page
    assert "<dt>Shots passed</dt><dd>1 of 3</dd>" in page
    assert "<dt>Generations</dt><dd>3</dd>" in page
    assert "<dt>Key message</dt>" in page
    assert page.count('<article class="card"') == 3
    assert [page.count(f'data-verdict="{v}"') for v in ("pass", "fail", "none")] == [1, 0, 2]
    assert page.count("<video ") == 2
    assert 'src="shot_1.mp4" poster="frames/shot_1_start.jpg"' in page
    assert "No clip yet" in page
    assert "2 attempt(s)" in page
    assert "<td>kling-3.0-pro</td><td>2</td>" in page  # per-model table
    # Agent-written text is escaped, never rendered as markup.
    assert "&lt;b&gt;warped&lt;/b&gt; hands &amp; a melted sign" in page
    assert "Hero shot &lt;needs&gt; detail &amp; polish" in page
    assert "<b>warped</b>" not in page
    assert "<script>" in page and "data-filter" in page


def test_report_for_an_unknown_run_is_refused() -> None:
    result = _call("build_report", {"run_id": "nope"})
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert "unknown run_id 'nope'" in content.text
