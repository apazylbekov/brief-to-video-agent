"""Generate one shot's clip: spend check, Higgsfield (live) or ffmpeg (mock), then the run log."""

import random
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import higgsfield_client
import httpx
from pydantic import BaseModel, Field

from app import config, higgsfield, run_log, shot_list
from app.errors import ReportableError

# Polling as docs.higgsfield.ai/docs/concepts/polling recommends: 2 s, growing 1.5x to 10 s.
POLL_FIRST_S = 2.0
POLL_MAX_S = 10.0
GENERATION_TIMEOUT_S = 20 * 60
# Worth retrying (docs: concepts/errors). The SDK already retries 408/429/5xx three times within
# a second; this adds longer backoff and covers network errors, which the SDK doesn't retry.
TRANSIENT_STATUS = frozenset({408, 423, 429, 500, 502, 503, 504})
RETRIES = 4
RETRY_FIRST_S = 2.0


class ClipError(ReportableError):
    """Generating a clip failed. The message says what happened and what it cost."""


class SpendCapError(ClipError):
    """The clip's estimate would take logged spend past MAX_SPEND_USD."""


class _GaveUp(ClipError):
    """Transient errors lasted through every retry, so the call's outcome is unknown."""


class GeneratedClip(BaseModel):
    run_id: str
    shot: int
    clip_path: str
    model: str
    mode: Literal["mock", "live"]
    attempt: int = Field(description="How many times this shot has been generated, this included")
    seconds: int
    latency_s: float
    cost_usd: float
    spent_usd: float = Field(description="Total spend logged in runs/runs.csv, this clip included")
    max_spend_usd: float


@dataclass
class _Charge:
    """What an attempt costs: the estimate once Higgsfield accepts it, and $0 again if it ends
    failed, nsfw or canceled (refunded). Unknown outcomes keep the estimate, so the cap errs
    on the safe side."""

    usd: Decimal = Decimal(0)


def generate(run_id: str, shot_index: int) -> GeneratedClip:
    """Generate one shot as runs/<run_id>/shot_<index>.mp4 and log the attempt."""
    shot = find_shot(run_id, shot_index)
    model = config.VIDEO_MODELS[shot.model]
    mode = config.video_mode()
    estimate = higgsfield.estimate_usd(model, shot.prompt, shot.duration_s, shot.image is not None)
    clip_path = path_for(run_id, shot.index)
    what = f"shot {shot.index} of run {run_id} ({model.id}, {shot.duration_s} s)"
    charge = _Charge()

    with _budget.reserve(estimate, what) as cap:
        attempt = run_log.next_attempt(run_id, shot.index)
        started = time.monotonic()
        try:
            if mode == "mock":
                _write_placeholder(clip_path, shot.duration_s)
            else:
                _generate_live(shot, model, estimate, clip_path, charge, what)
        finally:
            latency_s = round(time.monotonic() - started, 1)
            row = run_log.Row(
                run_id, shot.index, attempt, model.id, mode, shot.duration_s, latency_s, charge.usd
            )
            run_log.append(row)

    return GeneratedClip(
        run_id=run_id,
        shot=shot.index,
        clip_path=str(clip_path),
        model=model.id,
        mode=mode,
        attempt=attempt,
        seconds=shot.duration_s,
        latency_s=latency_s,
        cost_usd=float(charge.usd),
        spent_usd=float(run_log.total_spend()),
        max_spend_usd=float(cap),
    )


def path_for(run_id: str, shot_index: int) -> Path:
    return config.RUNS_DIR / run_id / f"shot_{shot_index}.mp4"


def find_shot(run_id: str, shot_index: int) -> shot_list.Shot:
    shots = shot_list.load(run_id).shots
    for shot in shots:
        if shot.index == shot_index:
            return shot
    indexes = ", ".join(str(s.index) for s in shots)
    raise ClipError(f"run {run_id} has shots {indexes}; there is no shot {shot_index}")


# --- Spend cap ------------------------------------------------------------------------------


class _Budget:
    """Checks MAX_SPEND_USD. Estimates of clips still generating count as spent, so calls
    running in parallel (MCPServer uses worker threads) can't overspend together."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = Decimal(0)

    @contextmanager
    def reserve(self, estimate: Decimal, what: str) -> Iterator[Decimal]:
        with self._lock:
            try:
                cap = config.max_spend_usd()
            except ValueError as exc:
                raise ClipError(str(exc)) from exc
            spent = run_log.total_spend()
            if spent + self._in_flight + estimate > cap:
                raise SpendCapError(_refusal(what, estimate, spent, self._in_flight, cap))
            self._in_flight += estimate
        try:
            yield cap
        finally:
            with self._lock:
                self._in_flight -= estimate


_budget = _Budget()


def _refusal(what: str, estimate: Decimal, spent: Decimal, in_flight: Decimal, cap: Decimal) -> str:
    still_generating = f" plus ${in_flight:.3f} for clips still generating" if in_flight else ""
    return (
        f"Refused, nothing generated: {what} is estimated at ${estimate:.3f}. "
        f"runs/runs.csv logs ${spent:.3f} spent{still_generating}, so the total would reach "
        f"${spent + in_flight + estimate:.3f}, over MAX_SPEND_USD=${cap:.2f}. Use the draft "
        "model or shorter shots, or ask the user to raise MAX_SPEND_USD."
    )


# --- Mock mode --------------------------------------------------------------------------------


def _write_placeholder(dest: Path, duration_s: int) -> None:
    """A 9:16 test-pattern clip of the shot's length, made locally for $0."""
    if shutil.which("ffmpeg") is None:
        raise ClipError("ffmpeg is not on PATH; mock mode needs it to write placeholder clips")
    partial = dest.with_suffix(".part.mp4")
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=720x1280:rate=24:duration={duration_s}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
        str(partial),
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ClipError(f"ffmpeg could not write the placeholder clip: {result.stderr[-300:]}")
    partial.replace(dest)


# --- Live mode --------------------------------------------------------------------------------


def _generate_live(
    shot: shot_list.Shot,
    model: config.VideoModel,
    estimate: Decimal,
    dest: Path,
    charge: _Charge,
    what: str,
) -> None:
    image_url = None
    if shot.image:
        image = config.ASSETS_DIR / shot.image
        image_url = _retry(lambda: _sdk_client().upload_file(image), f"uploading {shot.image}")
    path, body = higgsfield.video_request(model, shot.prompt, shot.duration_s, image_url)

    # One key for every try of this submit: if a response is lost and the submit is sent
    # again, Higgsfield returns the original request instead of starting (and billing) another.
    key = str(uuid.uuid4())
    charge.usd = estimate
    try:
        request = _retry(lambda: _sdk_client(key).submit(path, arguments=body), "submitting")
    except _GaveUp as exc:
        raise ClipError(
            f"{exc}. Higgsfield may have accepted it, so it is logged at the ${estimate:.3f} "
            "estimate; check your Higgsfield account before generating this shot again."
        ) from exc
    except ClipError:
        charge.usd = Decimal(0)  # rejected outright (bad input, no balance): nothing was created
        raise

    result = _wait(request)
    status = result.get("status")
    if status == "completed":
        url = (result.get("video") or {}).get("url")
        if not url:
            raise ClipError(f"Higgsfield finished {what} but returned no video URL: {result}")
        try:
            _retry(lambda: _download(url, dest), "downloading the clip")
        except ClipError as exc:
            raise ClipError(
                f"{exc}. The clip was generated and charged; download it from {url} within 7 days."
            ) from exc
        return

    charge.usd = Decimal(0)  # failed, nsfw and canceled generations are refunded
    if status == "nsfw":
        raise ClipError(
            f"Higgsfield's content moderation rejected {what} (nsfw). Not charged; logged at $0. "
            "Change the prompt or image in a new shot list, then generate again."
        )
    raise ClipError(
        f"Higgsfield generation of {what} ended {status}: "
        f"{result.get('error') or 'no reason given'} (request {request.request_id}). "
        "Not charged; logged at $0. Calling generate_clip again starts a new attempt."
    )


def _wait(request: higgsfield_client.SyncRequestController) -> dict[str, Any]:
    """Poll until the generation finishes, then fetch its final JSON."""
    delay = POLL_FIRST_S
    deadline = time.monotonic() + GENERATION_TIMEOUT_S
    while True:
        status = _retry(request.status, "checking the generation status")
        if isinstance(status, higgsfield_client.DONE_STATUSES):
            return _retry(request.get, "fetching the result")
        if time.monotonic() > deadline:
            raise ClipError(
                f"Higgsfield request {request.request_id} hasn't finished after "
                f"{GENERATION_TIMEOUT_S // 60} minutes. It may still complete and be charged, "
                "so it is logged at the estimate."
            )
        time.sleep(delay + random.uniform(0, 0.5))
        delay = min(delay * 1.5, POLL_MAX_S)


def _download(url: str, dest: Path) -> None:
    partial = dest.with_suffix(".part.mp4")
    with _download_client() as client, client.stream("GET", url) as response:
        response.raise_for_status()
        with partial.open("wb") as file:
            for chunk in response.iter_bytes():
                file.write(chunk)
    partial.replace(dest)


def _retry[T](call: Callable[[], T], what: str) -> T:
    """Run a Higgsfield call, retrying network errors and transient HTTP codes with backoff."""
    for attempt in range(1, RETRIES + 1):
        try:
            return call()
        except higgsfield_client.CredentialsMissedError as exc:
            raise ClipError(f"VIDEO_MODE=live needs Higgsfield credentials. {exc}") from exc
        except (httpx.HTTPError, higgsfield_client.HiggsfieldClientError) as exc:
            if not _is_transient(exc):
                raise ClipError(f"Higgsfield {what} failed: {exc}") from exc
            if attempt == RETRIES:
                raise _GaveUp(f"Higgsfield {what} failed {RETRIES} times: {exc}") from exc
            time.sleep(RETRY_FIRST_S * 2 ** (attempt - 1) + random.uniform(0, 0.5))
    raise AssertionError("unreachable")


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    # The SDK wraps HTTP errors in HiggsfieldClientError and keeps the response as the cause.
    cause = exc if isinstance(exc, httpx.HTTPStatusError) else exc.__cause__
    return (
        isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in TRANSIENT_STATUS
    )


def _sdk_client(idempotency_key: str | None = None) -> higgsfield_client.SyncClient:
    client = _new_sync_client()
    if idempotency_key:
        # submit() takes no headers, but the docs say to send Idempotency-Key with every
        # generation submit, so it goes on the SDK's own httpx session for this client.
        client._client.headers["Idempotency-Key"] = idempotency_key
    return client


def _new_sync_client() -> higgsfield_client.SyncClient:
    return higgsfield_client.SyncClient()  # reads HF_KEY when it first makes a request


def _download_client() -> httpx.Client:
    # The clip URL is a public CDN link: Higgsfield credentials must never be sent there.
    return httpx.Client(timeout=120, follow_redirects=True)
