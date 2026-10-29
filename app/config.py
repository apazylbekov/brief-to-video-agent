"""Settings and paths. Paths are module attributes so tests can monkeypatch them."""

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Explicit path: load_dotenv() with no argument searches from the caller's file, not the cwd.
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
BRIEFS_PATH = DATA_DIR / "briefs.json"
BRIEF_VECTORS_PATH = DATA_DIR / "brief_vectors.npy"

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
