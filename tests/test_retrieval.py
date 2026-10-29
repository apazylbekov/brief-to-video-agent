import json
import re
import zlib
from collections import Counter
from pathlib import Path
from typing import NoReturn

import numpy as np
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from app import config, retrieval


def _no_client(**kwargs: object) -> NoReturn:
    raise AssertionError("Gemini client should not be created")


# --- Dataset ------------------------------------------------------------------------------


def test_briefs_dataset_has_24_briefs_across_six_categories() -> None:
    briefs = retrieval.load_briefs()
    assert len(briefs) == 24
    assert len({b.id for b in briefs}) == 24
    assert len({b.brand for b in briefs}) == 24
    categories = Counter(b.id.rsplit("-", 1)[0] for b in briefs)
    assert categories == {c: 4 for c in ("food", "fitness", "travel", "tech", "fashion", "almaty")}
    for brief in briefs:
        assert all(str(v).strip() for v in brief.model_dump().values()), brief.id
        assert all(item.strip() for item in brief.must_include), brief.id


# --- Keyword fallback ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("coffee shop in Almaty", "almaty-01"),
        ("running app for beginners", "fitness-01"),
        ("noise cancelling earbuds for commuters", "tech-01"),
        ("vegan sneakers", "fashion-02"),
        ("ski rental", "almaty-03"),
    ],
)
def test_keyword_search_ranks_matching_brief_first(query: str, expected_id: str) -> None:
    result = retrieval.search(query)
    assert result.method == "keyword"
    assert result.matches[0].id == expected_id


def test_search_returns_k_matches_sorted_by_score() -> None:
    result = retrieval.search("healthy drink for runners", k=5)
    scores = [m.score for m in result.matches]
    assert len(scores) == 5
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= s <= 1 for s in scores)


def test_k_larger_than_catalog_returns_every_brief() -> None:
    assert len(retrieval.search("coffee", k=100).matches) == 24


def test_k_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="k must be at least 1"):
        retrieval.search("coffee", k=0)


def test_stopword_only_query_scores_zero() -> None:
    result = retrieval.search("the of and", k=2)
    assert [m.score for m in result.matches] == [0.0, 0.0]


@pytest.mark.parametrize("video_mode", ["mock", "live"])
def test_no_gemini_key_means_keyword_fallback_in_any_video_mode(
    monkeypatch: pytest.MonkeyPatch, video_mode: str
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VIDEO_MODE", video_mode)
    monkeypatch.setattr(retrieval.genai, "Client", _no_client)
    assert retrieval.search("coffee").method == "keyword"


# --- Embedding path, with a fake Gemini client ----------------------------------------------


def _bag_of_words(text: str, dims: int = 256) -> list[float]:
    vector = [0.0] * dims
    for word in re.findall(r"\w+", text.lower()):
        vector[zlib.crc32(word.encode()) % dims] += 1.0
    return vector


class FakeModels:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed_content(
        self, *, model: str, contents: list[types.Content]
    ) -> types.EmbedContentResponse:
        # gemini-embedding-2 merges a list of bare strings into ONE embedding.
        assert all(isinstance(c, types.Content) for c in contents)
        self.batch_sizes.append(len(contents))
        return types.EmbedContentResponse(
            embeddings=[
                types.ContentEmbedding(values=_bag_of_words(c.parts[0].text)) for c in contents
            ]
        )


class FakeClient:
    def __init__(self, models: FakeModels) -> None:
        self.models = models


@pytest.fixture
def fake_gemini(monkeypatch: pytest.MonkeyPatch) -> FakeModels:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    models = FakeModels()

    def make_client(api_key: str) -> FakeClient:
        assert api_key == "test-key"
        return FakeClient(models)

    monkeypatch.setattr(retrieval.genai, "Client", make_client)
    return models


@pytest.mark.parametrize("video_mode", ["mock", "live"])
def test_gemini_key_means_embeddings_in_any_video_mode(
    fake_gemini: FakeModels, monkeypatch: pytest.MonkeyPatch, video_mode: str
) -> None:
    monkeypatch.setenv("VIDEO_MODE", video_mode)
    assert retrieval.search("ski rental").method == "embedding"


def test_embeds_briefs_once_then_reuses_cache(fake_gemini: FakeModels) -> None:
    result = retrieval.search("ski rental", k=1)
    assert result.method == "embedding"
    assert result.matches[0].id == "almaty-03"
    assert fake_gemini.batch_sizes == [24, 1]  # all briefs, then the query
    assert np.load(config.BRIEF_VECTORS_PATH).shape[0] == 24

    retrieval.search("vegan sneakers", k=1)
    assert fake_gemini.batch_sizes == [24, 1, 1]  # cache hit: only the query


def test_cache_rebuilds_when_briefs_or_model_change(
    fake_gemini: FakeModels, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    briefs = json.loads(config.BRIEFS_PATH.read_text())[:2]
    briefs_path = tmp_path / "briefs.json"
    briefs_path.write_text(json.dumps(briefs))
    monkeypatch.setattr(config, "BRIEFS_PATH", briefs_path)

    retrieval.search("coffee")
    briefs[0]["key_message"] = "Now with extra fizz."
    briefs_path.write_text(json.dumps(briefs))
    retrieval.search("coffee")
    assert fake_gemini.batch_sizes == [2, 1, 2, 1]

    monkeypatch.setattr(config, "EMBED_MODEL", "another-embedding-model")
    retrieval.search("coffee")
    assert fake_gemini.batch_sizes == [2, 1, 2, 1, 2, 1]


def test_gemini_error_falls_back_to_keywords(
    fake_gemini: FakeModels, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rate_limited(**kwargs: object) -> NoReturn:
        raise genai_errors.APIError(429, {"error": {"message": "quota exceeded"}})

    monkeypatch.setattr(fake_gemini, "embed_content", rate_limited)
    assert retrieval.search("ski rental").method == "keyword"


# --- CLI ------------------------------------------------------------------------------------


def test_cli_prints_method_then_three_matches(capsys: pytest.CaptureFixture[str]) -> None:
    retrieval.main(["coffee", "shop", "in", "Almaty"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Method: keyword fallback (GEMINI_API_KEY not set)"
    assert len(lines) == 4
    assert lines[1].split()[0] == "almaty-01"


def test_cli_reports_gemini_embeddings(
    fake_gemini: FakeModels, capsys: pytest.CaptureFixture[str]
) -> None:
    retrieval.main(["ski rental"])
    assert capsys.readouterr().out.startswith("Method: Gemini embeddings (gemini-embedding-2)")


def test_blank_video_mode_means_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_MODE", "")
    assert config.video_mode() == "mock"
