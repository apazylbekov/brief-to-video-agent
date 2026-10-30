"""Clip QA: three frames for the agent to look at, and recording the verdict it reaches."""

import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app import clips, config, run_log, shot_list
from app.errors import ReportableError

POSITIONS: tuple[Literal["start", "middle", "end"], ...] = ("start", "middle", "end")
_lock = threading.Lock()  # record() rewrites shots.json, and tool calls can run in parallel


class ReviewError(ReportableError):
    """Reviewing a clip failed. The message says what to do next."""


class Frame(BaseModel):
    position: Literal["start", "middle", "end"]
    time_s: float
    path: str


class ReviewPacket(BaseModel):
    run_id: str
    shot: int
    attempt: int
    mode: str
    clip_path: str
    frames: list[Frame]
    spec: shot_list.Shot


class RecordedReview(BaseModel):
    run_id: str
    shot: int
    attempt: int
    verdict: Literal["pass", "fail"]
    reasons: str
    regeneration: shot_list.Regeneration
    next_step: str
    new_clip: clips.GeneratedClip | None = None


def frames(run_id: str, shot_index: int) -> ReviewPacket:
    """Extract the start, middle and end frames of a shot's clip to runs/<run_id>/frames/."""
    shot = clips.find_shot(run_id, shot_index)
    clip = clips.path_for(run_id, shot.index)
    if not clip.is_file():
        raise ReviewError(
            f"shot {shot.index} of run {run_id} has no clip yet; call generate_clip first"
        )
    _require("ffmpeg", "ffprobe")
    duration_s = _duration_s(clip)
    folder = config.RUNS_DIR / run_id / "frames"
    folder.mkdir(exist_ok=True)
    times = {"start": 0.0, "middle": duration_s / 2, "end": duration_s}
    extracted = []
    for position in POSITIONS:
        path = _frame_path(run_id, shot.index, position)
        _extract(clip, position, times[position], path)
        extracted.append(Frame(position=position, time_s=round(times[position], 2), path=str(path)))
    attempt, mode = _latest_generation(run_id, shot.index)
    return ReviewPacket(
        run_id=run_id,
        shot=shot.index,
        attempt=attempt,
        mode=mode,
        clip_path=str(clip),
        frames=extracted,
        spec=shot,
    )


def record(
    run_id: str, shot_index: int, verdict: Literal["pass", "fail"], reasons: str
) -> RecordedReview:
    """Log a verdict to runs.csv and shots.json; a shot's first fail gets one regeneration."""
    reasons = reasons.strip()
    if not reasons or "\n" in reasons or "\r" in reasons:
        raise ReviewError("reasons must be one non-empty line, e.g. 'warped hands in the middle'")
    shot = clips.find_shot(run_id, shot_index)
    clip = clips.path_for(run_id, shot.index)
    start = _frame_path(run_id, shot.index, "start")
    # The frames must come from the current clip, so a verdict always rests on a real look.
    if not clip.is_file() or not start.is_file() or start.stat().st_mtime < clip.stat().st_mtime:
        raise ReviewError(
            f"review_clip hasn't been run on shot {shot.index}'s current clip. Call review_clip, "
            "look at its frames, then record the verdict."
        )
    attempt, mode = _latest_generation(run_id, shot.index)
    latency_s = round(time.time() - start.stat().st_mtime, 1)  # frames extracted -> verdict
    reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")

    with _lock:
        run = shot_list.load(run_id)
        earlier = [r for r in run.reviews if r.shot == shot.index]
        review = shot_list.Review(
            shot=shot.index,
            attempt=attempt,
            verdict=verdict,
            reasons=reasons,
            reviewed_at=reviewed_at,
        )
        run.reviews.append(review)
        shot_list.save_run(run_id, run)
    run_log.append(
        run_log.Row(
            run_id,
            shot.index,
            attempt,
            shot.model,
            mode,
            shot.duration_s,
            latency_s,
            Decimal(0),  # the agent looks at the frames itself; nothing is billed here
            event="review",
            verdict=verdict,
            reasons=reasons,
        )
    )

    regeneration, new_clip, next_step = _act_on(run_id, shot.index, verdict, earlier)
    with _lock:
        run = shot_list.load(run_id)
        for stored in run.reviews:
            if stored.shot == shot.index and stored.reviewed_at == reviewed_at:
                stored.regeneration = regeneration
        shot_list.save_run(run_id, run)

    return RecordedReview(
        run_id=run_id,
        shot=shot.index,
        attempt=attempt,
        verdict=verdict,
        reasons=reasons,
        regeneration=regeneration,
        next_step=next_step,
        new_clip=new_clip,
    )


def _act_on(
    run_id: str, shot: int, verdict: str, earlier: list[shot_list.Review]
) -> tuple[shot_list.Regeneration, clips.GeneratedClip | None, str]:
    """A pass ends the shot. A first fail gets one regeneration, within the spend cap."""
    if verdict == "pass":
        return "not_needed", None, f"Recorded: shot {shot} passed."
    if any(r.regeneration in ("regenerated", "generation_failed") for r in earlier):
        return (
            "already_used",
            None,
            f"Recorded: shot {shot} failed again after its one regeneration; it stays failed.",
        )
    try:
        clip = clips.generate(run_id, shot)
    except clips.SpendCapError as exc:
        return (
            "blocked_by_spend_cap",
            None,
            f"Recorded: shot {shot} failed and the spend cap blocks its regeneration, so it "
            f"stays failed. {exc}",
        )
    except ReportableError as exc:
        return (
            "generation_failed",
            None,
            f"Recorded: shot {shot} failed, and its one regeneration failed too ({exc}). "
            "It stays failed.",
        )
    return (
        "regenerated",
        clip,
        f"Recorded: shot {shot} failed and has been regenerated once (attempt {clip.attempt}, "
        f"${clip.cost_usd:.3f}). Call review_clip on it and record the new verdict; this was "
        "its only regeneration.",
    )


def _latest_generation(run_id: str, shot: int) -> tuple[int, str]:
    """Attempt number and mode of the shot's latest generation, i.e. the clip being reviewed."""
    generations = run_log.generations(run_id, shot)
    if not generations:
        return 0, config.video_mode()
    return int(generations[-1]["attempt"]), generations[-1]["mode"]


def _frame_path(run_id: str, shot: int, position: str) -> Path:
    return config.RUNS_DIR / run_id / "frames" / f"shot_{shot}_{position}.jpg"


def _require(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise ReviewError(f"{' and '.join(missing)} not on PATH; review_clip needs them")


def _duration_s(clip: Path) -> float:
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0"]
    result = subprocess.run([*command, str(clip)], capture_output=True, text=True, check=False)
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise ReviewError(f"ffprobe could not read {clip.name}: {result.stderr[-300:]}") from exc


def _extract(clip: Path, position: str, at_s: float, dest: Path) -> None:
    if position == "end":
        # Seeking to the exact end can land past the last frame, so decode the final second and
        # keep overwriting one image: what's left is the last frame.
        source = ["-sseof", "-1", "-i", str(clip), "-update", "1"]
    else:
        source = ["-ss", f"{at_s:.3f}", "-i", str(clip), "-frames:v", "1"]
    command = ["ffmpeg", "-y", "-v", "error", *source, "-q:v", "2", str(dest)]
    dest.unlink(missing_ok=True)  # so a stale frame from an earlier clip can't pass the check
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not dest.is_file():
        raise ReviewError(
            f"ffmpeg could not extract the {position} frame of {clip.name}: {result.stderr[-300:]}"
        )
