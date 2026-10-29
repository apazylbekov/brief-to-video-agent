import asyncio
import json

import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from app import config, higgsfield, shot_list
from app.server import server


def _shot(index: int, **changes: object) -> dict[str, object]:
    shot: dict[str, object] = {
        "index": index,
        "duration_s": 5,
        "subject": "barista pouring a flat white",
        "setting": "Almaty café counter, snowy peaks through the window",
        "camera_move": "slow dolly in",
        "lens": "50mm",
        "lighting": "soft morning window light",
        "mood": "warm and unhurried",
        "prompt": "Vertical close-up of a barista pouring latte art into a flat white, snowy "
        "mountains blurred through the café window, soft morning light, slow dolly in.",
        "model": "kling-3.0-standard",
        "model_reason": "Cheap draft to check framing before a quality pass.",
    }
    return shot | changes


def _shots(count: int = 3, duration_s: int = 5) -> list[dict[str, object]]:
    return [_shot(i, duration_s=duration_s) for i in range(1, count + 1)]


def _call(arguments: dict[str, object]) -> CallToolResult:
    """Call save_shot_list the way an agent does: over MCP, through an in-memory client."""

    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool("save_shot_list", arguments)

    return asyncio.run(call())


def _error_text(result: CallToolResult) -> str:
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


def _saved_runs() -> list[str]:
    return sorted(p.name for p in config.RUNS_DIR.iterdir()) if config.RUNS_DIR.exists() else []


# --- The four requested cases -------------------------------------------------------------


def test_valid_shot_list_is_saved_and_priced() -> None:
    shots = _shots()
    shots[2] = _shot(3, duration_s=6, model="kling-3.0-pro", model_reason="Hero shot.")

    result = _call({"brief_id": "almaty-01", "shots": shots})

    assert not result.is_error
    data = result.structured_content
    assert data is not None
    assert data["pricing"] == "mock"
    # $0.0714/s and $0.0952/s, rounded up like Higgsfield: 6 s of Pro is $0.5712 -> $0.572.
    assert [s["usd"] for s in data["shots"]] == [0.357, 0.357, 0.572]
    assert data["total_usd"] == 1.286
    assert data["total_duration_s"] == 16
    assert _saved_runs() == [data["run_id"]]
    saved = json.loads((config.RUNS_DIR / data["run_id"] / "shots.json").read_text())
    assert saved["brief_id"] == "almaty-01"
    assert [s["model"] for s in saved["shots"]] == ["kling-3.0-standard"] * 2 + ["kling-3.0-pro"]


@pytest.mark.parametrize(
    ("shots", "expected"),
    [
        (_shots(count=5), "- shots: List should have at most 4 items"),
        (_shots(count=4, duration_s=6), "- shots: total duration is 24s, over the 20s limit"),
    ],
    ids=["five shots", "24 seconds"],
)
def test_too_long_shot_list_is_rejected(shots: list[dict[str, object]], expected: str) -> None:
    text = _error_text(_call({"brief_id": "almaty-01", "shots": shots}))
    assert expected in text
    assert _saved_runs() == []


def test_unknown_model_is_rejected_with_the_valid_choices() -> None:
    shots = _shots()
    shots[1]["model"] = "sora-2"
    text = _error_text(_call({"brief_id": "almaty-01", "shots": shots}))
    assert (
        "- shots[1].model: unknown model 'sora-2'; "
        "use 'kling-3.0-standard' (draft) or 'kling-3.0-pro' (quality)"
    ) in text
    assert _saved_runs() == []


def test_missing_field_is_rejected() -> None:
    shots = _shots()
    del shots[0]["lens"]
    text = _error_text(_call({"brief_id": "almaty-01", "shots": shots}))
    assert "- shots[0].lens: Field required" in text
    assert _saved_runs() == []


# --- Error format, images, live estimates ---------------------------------------------------


def test_every_problem_is_reported_in_one_message() -> None:
    shots = _shots()
    del shots[0]["lens"]
    shots[1]["model"] = "sora-2"
    shots[2]["duration_s"] = 12
    shots[2]["camera"] = "handheld"

    text = _error_text(_call({"brief_id": "nope", "shots": shots}))

    assert text.startswith(
        "Error executing tool save_shot_list: Shot list rejected, nothing saved. "
        "Fix all 5 problem(s)"
    )
    assert "- brief_id: unknown brief_id 'nope'; use an id returned by search_briefs" in text
    assert "- shots[0].lens: Field required" in text
    assert "- shots[1].model: unknown model 'sora-2'" in text
    assert "- shots[2].duration_s: Input should be less than or equal to 8 (got 12)" in text
    assert "- shots[2].camera: unknown field; allowed fields are index, duration_s," in text


@pytest.fixture
def frame() -> str:
    config.ASSETS_DIR.mkdir()
    (config.ASSETS_DIR / "frame.jpg").write_bytes(b"\xff\xd8\xff")
    return "frame.jpg"


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("missing.jpg", "'missing.jpg' is not in assets/; available images: ['frame.jpg']"),
        ("../frame.jpg", "'../frame.jpg' is outside assets/"),
        ("notes.txt", "'notes.txt' is not an image"),
    ],
)
def test_image_must_be_an_image_file_in_assets(frame: str, image: str, expected: str) -> None:
    shots = _shots()
    shots[0]["image"] = image
    with pytest.raises(shot_list.ShotListError) as excinfo:
        shot_list.save("almaty-01", shots)
    assert f"- shots[0].image: {expected}" in str(excinfo.value)


def test_live_mode_prices_each_shot_with_the_estimate_endpoint(
    monkeypatch: pytest.MonkeyPatch, frame: str
) -> None:
    monkeypatch.setenv("VIDEO_MODE", "live")
    monkeypatch.setenv("HF_KEY", "key-id:key-secret")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"type": "estimate", "usd": "0.400"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        higgsfield,
        "_http",
        lambda: httpx.Client(base_url=config.HIGGSFIELD_URL, transport=transport),
    )
    shots = _shots()
    shots[2] = _shot(3, model="kling-3.0-pro", image=frame)

    saved = shot_list.save("almaty-01", shots)

    assert saved.pricing == "higgsfield"
    assert saved.total_usd == 1.2
    assert [r.url.path for r in requests] == [
        "/estimate/kling-video/v3.0/std/text-to-video",
        "/estimate/kling-video/v3.0/std/text-to-video",
        "/estimate/kling-video/v3.0/pro/image-to-video",
    ]
    assert requests[0].headers["Authorization"] == "Key key-id:key-secret"
    text_body, image_body = json.loads(requests[0].content), json.loads(requests[2].content)
    assert text_body == {
        "prompt": shots[0]["prompt"],
        "duration": 5,
        "sound": "off",
        "aspect_ratio": "9:16",
    }
    assert image_body["image_url"] == higgsfield.ESTIMATE_IMAGE_URL
    assert "aspect_ratio" not in image_body  # image-to-video follows the image's shape


def test_live_mode_without_credentials_says_what_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_MODE", "live")
    text = _error_text(_call({"brief_id": "almaty-01", "shots": _shots()}))
    assert "VIDEO_MODE=live needs Higgsfield credentials" in text
    assert "HF_KEY" in text
    assert _saved_runs() == []
