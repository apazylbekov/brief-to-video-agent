import asyncio
import json
import re
import subprocess
import time
from decimal import Decimal

import higgsfield_client
import httpx
import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from app import clips, config, higgsfield, run_log, shot_list, smoke
from app.server import server
from tests.factories import make_shots


def _call(arguments: dict[str, object]) -> CallToolResult:
    """Call generate_clip the way an agent does: over MCP, through an in-memory client."""

    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool("generate_clip", arguments)

    return asyncio.run(call())


def _error_text(result: CallToolResult) -> str:
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


@pytest.fixture
def run_id() -> str:
    """A saved run of three 5 s shots on the draft model."""
    return shot_list.save("almaty-01", make_shots()).run_id


# --- Mock mode ------------------------------------------------------------------------------


def test_mock_clip_is_a_placeholder_logged_at_zero(run_id: str) -> None:
    result = _call({"run_id": run_id, "shot_index": 2})

    assert not result.is_error
    clip = result.structured_content
    assert clip is not None
    assert clip["clip_path"] == str(config.RUNS_DIR / run_id / "shot_2.mp4")
    assert (clip["mode"], clip["model"], clip["attempt"], clip["seconds"]) == (
        "mock",
        "kling-3.0-standard",
        1,
        5,
    )
    assert (clip["cost_usd"], clip["spent_usd"], clip["max_spend_usd"]) == (0, 0, 8)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries",
         "format=duration:stream=width,height", clip["clip_path"]],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    info = json.loads(probe.stdout)
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (720, 1280)
    assert float(info["format"]["duration"]) == pytest.approx(5, abs=0.1)
    [row] = run_log.rows()
    assert list(row) == list(run_log.FIELDS)
    assert {k: row[k] for k in ("run_id", "shot", "attempt", "model", "mode", "cost_usd")} == {
        "run_id": run_id,
        "shot": "2",
        "attempt": "1",
        "model": "kling-3.0-standard",
        "mode": "mock",
        "cost_usd": "0.000",
    }


def test_every_call_appends_a_row_and_attempts_count_per_shot(run_id: str) -> None:
    for index in (1, 1, 3):
        clips.generate(run_id, index)
    assert [(r["shot"], r["attempt"]) for r in run_log.rows()] == [
        ("1", "1"),
        ("1", "2"),
        ("3", "1"),
    ]


@pytest.mark.parametrize(
    ("spent", "cap", "expected"),
    [
        ("7.700", None, "would reach $8.057, over MAX_SPEND_USD=$8.00"),
        ("0", "0.30", "would reach $0.357, over MAX_SPEND_USD=$0.30"),
    ],
    ids=["earlier spend", "low cap"],
)
def test_spend_cap_refuses_before_generating(
    run_id: str, monkeypatch: pytest.MonkeyPatch, spent: str, cap: str | None, expected: str
) -> None:
    if cap:
        monkeypatch.setenv("MAX_SPEND_USD", cap)
    earlier = run_log.Row("earlier-run", 1, 1, "kling-3.0-pro", "live", 8, 95.0, Decimal(spent))
    run_log.append(earlier)

    text = _error_text(_call({"run_id": run_id, "shot_index": 1}))

    assert f"Refused, nothing generated: shot 1 of run {run_id} (kling-3.0-standard, 5 s)" in text
    assert "is estimated at $0.357" in text
    assert expected in text
    assert not (config.RUNS_DIR / run_id / "shot_1.mp4").exists()
    assert len(run_log.rows()) == 1  # a refused call is not an attempt


@pytest.mark.parametrize(
    ("run", "shot", "expected"),
    [
        ("nope", 1, "unknown run_id 'nope'; use the run_id that save_shot_list returned"),
        ("../../etc", 1, "unknown run_id '../../etc'"),
        ("<saved>", 9, "has shots 1, 2, 3; there is no shot 9"),
    ],
)
def test_unknown_run_or_shot_is_explained(run_id: str, run: str, shot: int, expected: str) -> None:
    run = run_id if run == "<saved>" else run
    assert expected in _error_text(_call({"run_id": run, "shot_index": shot}))
    assert run_log.rows() == []


def test_smoke_saves_one_draft_shot_and_generates_it(capsys: pytest.CaptureFixture[str]) -> None:
    smoke.main(["a", "barista", "pouring", "latte", "art"])

    out = capsys.readouterr().out
    assert out.startswith("VIDEO_MODE=mock: generating one 5 s clip with kling-3.0-standard")
    match = re.search(r"run_id:\s+(\S+)", out)
    assert match is not None
    [shot] = shot_list.load(match[1]).shots
    assert (shot.prompt, shot.model, shot.duration_s) == (
        "a barista pouring latte art",
        "kling-3.0-standard",
        5,
    )
    assert (config.RUNS_DIR / match[1] / "shot_1.mp4").is_file()
    assert "cost:     $0.000" in out


# --- Live mode, against a fake Higgsfield ----------------------------------------------------

CDN = "https://cdn.example.com"


class FakeHiggsfield:
    """Stands in for api.higgsfield.ai, its upload storage and its CDN; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.submit_errors: list[int | type[httpx.TransportError]] = []  # served before success
        self.final: dict[str, object] = {"status": "completed", "video": {"url": f"{CDN}/1.mp4"}}
        self.polls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "cdn.example.com":
            return httpx.Response(200, content=b"fake mp4 bytes")
        if url.host == "uploads.example.com":
            return httpx.Response(200)
        if url.path.startswith("/estimate/"):
            return httpx.Response(200, json={"type": "estimate", "usd": "0.357"})
        if url.path == "/files/generate-upload-url":
            upload = {
                "public_url": f"{CDN}/up/frame.jpg",
                "upload_url": "https://uploads.example.com/x",
            }
            return httpx.Response(200, json=upload)
        if url.path == "/requests/req-1/status":
            self.polls += 1
            status = {"status": "in_progress"} if self.polls == 1 else self.final
            return httpx.Response(200, json={"request_id": "req-1", **status})
        # Anything else is a generation submit.
        if self.submit_errors:
            error = self.submit_errors.pop(0)
            if isinstance(error, int):
                return httpx.Response(error, json={"detail": f"HTTP {error} from the fake"})
            raise error("connection dropped", request=request)
        base = f"{config.HIGGSFIELD_URL}/requests/req-1"
        accepted = {"status": "queued", "request_id": "req-1", "status_url": f"{base}/status"}
        return httpx.Response(200, json=accepted | {"cancel_url": f"{base}/cancel"})

    def submits(self) -> list[httpx.Request]:
        api = [r for r in self.requests if r.method == "POST" and r.url.host == "api.higgsfield.ai"]
        return [r for r in api if not r.url.path.startswith(("/estimate/", "/files/"))]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeHiggsfield:
    """Live mode with the real higgsfield_client SDK, its HTTP pointed at FakeHiggsfield."""
    monkeypatch.setenv("VIDEO_MODE", "live")
    monkeypatch.setenv("HF_KEY", "key-id:key-secret")
    monkeypatch.setattr(time, "sleep", lambda seconds: None)  # backoff and polling waits
    fake = FakeHiggsfield()
    transport = httpx.MockTransport(fake.handle)

    def sync_client() -> higgsfield_client.SyncClient:
        client = higgsfield_client.SyncClient()
        auth = {"Authorization": "Key key-id:key-secret"}
        # The SDK's httpx clients are cached properties; seed them with the fake transport.
        client.__dict__["_client"] = httpx.Client(
            base_url=config.HIGGSFIELD_URL, headers=auth, transport=transport
        )
        client.__dict__["_upload_client"] = httpx.Client(transport=transport)
        return client

    monkeypatch.setattr(clips, "_new_sync_client", sync_client)
    monkeypatch.setattr(clips, "_download_client", lambda: httpx.Client(transport=transport))
    monkeypatch.setattr(
        higgsfield,
        "_http",
        lambda: httpx.Client(base_url=config.HIGGSFIELD_URL, transport=transport),
    )
    return fake


def test_live_retries_reuse_one_idempotency_key_then_poll_and_download(
    fake: FakeHiggsfield, run_id: str
) -> None:
    fake.submit_errors = [httpx.ConnectError, 503]  # a dropped connection, then a busy server

    clip = clips.generate(run_id, 1)

    submits = fake.submits()
    assert [r.url.path for r in submits] == ["/kling-video/v3.0/std/text-to-video"] * 3
    assert len({r.headers["Idempotency-Key"] for r in submits}) == 1  # so no second clip is made
    assert json.loads(submits[-1].content) == {
        "prompt": make_shots()[0]["prompt"],
        "duration": 5,
        "sound": "off",
        "aspect_ratio": "9:16",
    }
    assert fake.polls >= 2
    assert (config.RUNS_DIR / run_id / "shot_1.mp4").read_bytes() == b"fake mp4 bytes"
    assert (clip.mode, clip.cost_usd, clip.spent_usd) == ("live", 0.357, 0.357)
    [row] = run_log.rows()
    assert (row["mode"], row["attempt"], row["cost_usd"]) == ("live", "1", "0.357")


def test_live_image_to_video_uploads_the_asset_first(fake: FakeHiggsfield) -> None:
    config.ASSETS_DIR.mkdir()
    (config.ASSETS_DIR / "frame.jpg").write_bytes(b"\xff\xd8\xff")
    shots = make_shots()
    shots[0] |= {"image": "frame.jpg", "model": "kling-3.0-pro"}
    run_id = shot_list.save("almaty-01", shots).run_id

    clips.generate(run_id, 1)

    [upload] = [r for r in fake.requests if r.url.host == "uploads.example.com"]
    assert upload.content == b"\xff\xd8\xff"
    assert "Authorization" not in upload.headers  # credentials never go to the storage URL
    [submit] = fake.submits()
    assert submit.url.path == "/kling-video/v3.0/pro/image-to-video"
    body = json.loads(submit.content)
    assert body["image_url"] == f"{CDN}/up/frame.jpg"
    assert "aspect_ratio" not in body


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        ({"status": "failed", "error": "Generation failed"}, "ended failed: Generation failed"),
        ({"status": "nsfw"}, "content moderation rejected shot 1"),
    ],
    ids=["failed", "nsfw"],
)
def test_live_generation_that_fails_is_logged_at_zero(
    fake: FakeHiggsfield, run_id: str, final: dict[str, object], expected: str
) -> None:
    fake.final = final

    text = _error_text(_call({"run_id": run_id, "shot_index": 1}))

    assert expected in text
    assert "Not charged; logged at $0" in text
    [row] = run_log.rows()
    assert (row["mode"], row["cost_usd"]) == ("live", "0.000")
    assert not (config.RUNS_DIR / run_id / "shot_1.mp4").exists()


def test_live_submit_rejected_for_balance_is_not_retried_and_costs_nothing(
    fake: FakeHiggsfield, run_id: str
) -> None:
    fake.submit_errors = [403]  # "Insufficient balance" in Higgsfield's error table

    text = _error_text(_call({"run_id": run_id, "shot_index": 1}))

    assert "Higgsfield submitting failed: HTTP 403 from the fake" in text
    assert len(fake.submits()) == 1
    assert run_log.rows()[0]["cost_usd"] == "0.000"
