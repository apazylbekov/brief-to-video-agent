"""Settings and paths. Paths are module attributes so tests can monkeypatch them."""

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Explicit path: load_dotenv() with no argument searches from the caller's file, not the cwd.
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
BRIEFS_PATH = DATA_DIR / "briefs.json"
BRIEF_VECTORS_PATH = DATA_DIR / "brief_vectors.npy"
RUNS_DIR = ROOT / "runs"
ASSETS_DIR = ROOT / "assets"

HIGGSFIELD_URL = "https://api.higgsfield.ai"


@dataclass(frozen=True)
class VideoModel:
    id: str  # what a shot's `model` field names
    tier: Literal["draft", "quality"]
    text_to_video: str  # Higgsfield model paths
    image_to_video: str
    usd_per_second: Decimal  # mock-mode price


# Kling 3.0 Standard and Pro share one input schema: integer duration 3-15 s and sound on/off;
# text-to-video takes aspect_ratio, image-to-video takes image_url and follows the image's shape.
# Paths checked against docs.higgsfield.ai on 2026-10-29. Prices are the estimate endpoint's for
# 9:16 with sound off on that date; they are linear in duration and the same for both workflows.
DRAFT_MODEL = VideoModel(
    id="kling-3.0-standard",
    tier="draft",
    text_to_video="kling-video/v3.0/std/text-to-video",
    image_to_video="kling-video/v3.0/std/image-to-video",
    usd_per_second=Decimal("0.0714"),
)
QUALITY_MODEL = VideoModel(
    id="kling-3.0-pro",
    tier="quality",
    text_to_video="kling-video/v3.0/pro/text-to-video",
    image_to_video="kling-video/v3.0/pro/image-to-video",
    usd_per_second=Decimal("0.0952"),
)
VIDEO_MODELS = {m.id: m for m in (DRAFT_MODEL, QUALITY_MODEL)}

ASPECT_RATIO = "9:16"  # every brief targets a vertical platform
SOUND = "off"  # native audio adds 50% to the price, and clip QA judges the picture

# Confirmed with client.models.list() on 2026-10-29; free tier. Takes task hints as text
# prefixes rather than task_type, and its vectors are incompatible with gemini-embedding-001.
EMBED_MODEL = "gemini-embedding-2"


def video_mode() -> Literal["mock", "live"]:
    """Read VIDEO_MODE at call time so tests can switch it with monkeypatch.setenv."""
    # `or`, not a get() default: a blank `VIDEO_MODE=` line in .env means unset.
    mode = (os.environ.get("VIDEO_MODE") or "mock").strip().lower()
    if mode not in ("mock", "live"):
        raise ValueError(f"VIDEO_MODE must be 'mock' or 'live', got {mode!r}")
    return mode


def max_spend_usd() -> Decimal:
    """MAX_SPEND_USD at call time: the cap on total spend logged in runs/runs.csv."""
    raw = (os.environ.get("MAX_SPEND_USD") or "8").strip()
    try:
        cap = Decimal(raw)
    except InvalidOperation:
        cap = Decimal("NaN")
    if not cap.is_finite() or cap < 0:
        raise ValueError(f"MAX_SPEND_USD must be a number of dollars, 0 or more; got {raw!r}")
    return cap
