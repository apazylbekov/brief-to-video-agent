"""Find the creative briefs that best match a request.

With GEMINI_API_KEY set, briefs are ranked by Gemini embedding cosine similarity, whatever the
VIDEO_MODE. Without it, they are ranked by keyword overlap so everything works offline.

CLI: uv run python -m app.retrieval coffee shop in Almaty
"""

import argparse
import hashlib
import json
import logging
import os
import re
from typing import Literal

import httpx
import numpy as np
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, TypeAdapter

from app import config

log = logging.getLogger(__name__)


class Brief(BaseModel):
    id: str
    brand: str
    product: str
    audience: str
    goal: str
    tone: str
    key_message: str
    must_include: list[str]
    platform: str


class BriefMatch(Brief):
    score: float


class SearchResult(BaseModel):
    method: Literal["embedding", "keyword"]
    matches: list[BriefMatch]


_BRIEFS = TypeAdapter(list[Brief])


def load_briefs() -> list[Brief]:
    return _BRIEFS.validate_json(config.BRIEFS_PATH.read_bytes())


def search(query: str, k: int = 3) -> SearchResult:
    """Return the top k briefs for the query, best first."""
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    briefs = load_briefs()
    scores = _embedding_scores(query, briefs)
    method: Literal["embedding", "keyword"] = "embedding"
    if scores is None:
        scores, method = keyword_scores(query, briefs), "keyword"
    # Stable sort keeps file order among ties, so results are deterministic.
    top = np.argsort(-scores, kind="stable")[:k]
    return SearchResult(
        method=method,
        matches=[
            BriefMatch(**briefs[i].model_dump(), score=round(float(scores[i]), 4)) for i in top
        ],
    )


# --- Keyword fallback ---------------------------------------------------------------------

_STOPWORDS = frozenset(
    "a an and are as at be by for from in is it me my of on or our that the this to we "
    "with you your".split()
)


def _stem(token: str) -> str:
    # Crude plural folding (sneakers -> sneaker); applied to both sides, so it is consistent.
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1}


def keyword_scores(query: str, briefs: list[Brief]) -> np.ndarray:
    """Fraction of the query's keywords found in each brief, in [0, 1]."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return np.zeros(len(briefs))
    return np.array(
        [
            len(query_tokens & _tokens(" ".join(_field_values(b)))) / len(query_tokens)
            for b in briefs
        ]
    )


def _field_values(brief: Brief) -> list[str]:
    return [
        brief.brand,
        brief.product,
        brief.audience,
        brief.goal,
        brief.tone,
        brief.key_message,
        *brief.must_include,
        brief.platform,
    ]


# --- Gemini embeddings --------------------------------------------------------------------


def _client() -> genai.Client | None:
    # Ignores VIDEO_MODE, which only governs video generation. The key is passed explicitly
    # because genai.Client() on its own would prefer GOOGLE_API_KEY when both are set.
    api_key = os.environ.get("GEMINI_API_KEY")
    return genai.Client(api_key=api_key) if api_key else None


def _embedding_scores(query: str, briefs: list[Brief]) -> np.ndarray | None:
    """Cosine similarity of the query to each brief, or None to use the keyword fallback."""
    client = _client()
    if client is None:
        return None
    try:
        brief_vectors = _brief_vectors(client, briefs)
        query_vector = _embed(client, [f"task: search result | query: {query}"])[0]
    except (genai_errors.APIError, httpx.HTTPError) as exc:
        log.warning("Gemini embedding failed (%s); brief search uses keyword scores", exc)
        return None
    return brief_vectors @ query_vector


def _document_text(brief: Brief) -> str:
    # gemini-embedding-2's documented retrieval format for documents.
    body = (
        f"Product: {brief.product}. Audience: {brief.audience}. Goal: {brief.goal}. "
        f"Tone: {brief.tone}. Key message: {brief.key_message} "
        f"Must include: {'; '.join(brief.must_include)}. Platform: {brief.platform}."
    )
    return f"title: {brief.brand} | text: {body}"


def _embed(client: genai.Client, texts: list[str]) -> np.ndarray:
    """Embed each text separately and return unit-length rows."""
    # A plain list of strings is embedded as ONE combined input by gemini-embedding-2;
    # wrapping each text in its own Content returns one vector per text.
    response = client.models.embed_content(
        model=config.EMBED_MODEL,
        contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
    )
    vectors = np.array([e.values for e in response.embeddings or []], dtype=np.float32)
    if len(vectors) != len(texts):
        raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(vectors)}")
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def _brief_vectors(client: genai.Client, briefs: list[Brief]) -> np.ndarray:
    """Load cached brief vectors, re-embedding if the briefs or the model changed."""
    texts = [_document_text(b) for b in briefs]
    fingerprint = hashlib.sha256(json.dumps([config.EMBED_MODEL, *texts]).encode()).hexdigest()
    vectors_path = config.BRIEF_VECTORS_PATH
    meta_path = vectors_path.with_suffix(".json")
    if vectors_path.exists() and meta_path.exists():
        if json.loads(meta_path.read_text()).get("fingerprint") == fingerprint:
            return np.load(vectors_path)

    vectors = _embed(client, texts)
    vectors_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(vectors_path, vectors)
    # Written last, so an interrupted save leaves a mismatch and triggers a re-embed.
    meta = {"model": config.EMBED_MODEL, "count": len(texts), "fingerprint": fingerprint}
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return vectors


# --- CLI ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    # No load_dotenv() here: importing app.config already loaded .env via python-dotenv.
    parser = argparse.ArgumentParser(prog="python -m app.retrieval", description=__doc__)
    parser.add_argument("query", nargs="+", help="the request to match (quotes optional)")
    query = " ".join(parser.parse_args(argv).query)

    result = search(query, k=3)
    if result.method == "embedding":
        print(f"Method: Gemini embeddings ({config.EMBED_MODEL})")
    elif os.environ.get("GEMINI_API_KEY"):
        print("Method: keyword fallback (Gemini call failed; see warning above)")
    else:
        print("Method: keyword fallback (GEMINI_API_KEY not set)")
    for match in result.matches:
        print(f"  {match.id:<12}{match.brand:<24}{match.score:.4f}")


if __name__ == "__main__":
    main()
