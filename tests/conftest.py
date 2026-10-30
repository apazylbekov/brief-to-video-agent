"""Every test runs offline: mock mode, no API keys, outputs under tmp_path."""

from pathlib import Path

import pytest

from app import config

# config loads .env on import, so strip anything it could have put in the environment.
# Unsetting GEMINI_API_KEY is what forces brief search onto the keyword fallback: unlike
# video generation, it does not check VIDEO_MODE.
API_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "HF_KEY", "HF_API_KEY", "HF_API_SECRET")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in API_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VIDEO_MODE", "mock")
    monkeypatch.delenv("MAX_SPEND_USD", raising=False)  # tests get the default $8 cap
    monkeypatch.setattr(config, "BRIEF_VECTORS_PATH", tmp_path / "brief_vectors.npy")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "ASSETS_DIR", tmp_path / "assets")
