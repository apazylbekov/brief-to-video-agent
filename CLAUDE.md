# brief-to-video-agent

## Goal

One command turns a one-line request into:

1. a matched creative brief,
2. a 3-shot list,
3. clips generated through Higgsfield's API,
4. a QA verdict per clip,
5. an HTML report,

logging cost and latency per model along the way.

## Commands

```bash
uv sync                      # install deps into .venv (Python 3.12, pinned in .python-version)
uv run pytest                # all tests; must pass with no API keys set
uv run ruff check .          # lint (includes ANN: type hints required)
uv run ruff format .         # format
uv add <pkg>                 # add a dependency; never pip install
gitleaks git --redact        # scan commit history for secrets before pushing
git config core.hooksPath .githooks   # once per clone: gitleaks pre-commit hook
```

System tools outside uv, expected on PATH: `ffmpeg`/`ffprobe` (video) and `gitleaks` (secrets).

## Spec

### MCP server — `app/server.py`

- Built on the MCP Python SDK 2.x: `from mcp.server.mcpserver import MCPServer`.
  FastMCP was renamed to MCPServer in 2.x; do not import FastMCP.
- Tools:
  - `search_briefs`
  - `save_shot_list`
  - `generate_clip`
  - `review_clip`
  - `record_review`
  - `build_report`

### Video — Higgsfield API

- API docs: https://docs.higgsfield.ai/docs
- `app/config.py` defines two models from Higgsfield's catalog: a cheap **draft** model and a
  higher-quality one. Verify model IDs against the docs when choosing them.
- Call the cost-estimate endpoint before every generation.

### Modes and spend cap

- `VIDEO_MODE=mock` (default) costs $0: no network calls to Higgsfield or Gemini, no API keys
  read. `VIDEO_MODE=live` calls the real APIs.
- `MAX_SPEND_USD` (default 8) caps Higgsfield spend. Before each live generation, refuse it if
  spend so far in the run plus that clip's estimate would exceed the cap.

### Run log — `runs/runs.csv`

Every generation and every review appends one row, including model, cost (USD) and latency,
so cost and latency can be reported per model.

## Rules

- Type hints on every function, argument and return value, tests included (ruff `ANN` enforces).
- Every MCP tool has at least one pytest test.
- No test needs an API key. Tests run in mock mode, `monkeypatch.delenv` any keys they could
  pick up from `.env`, and point the runs directory at `tmp_path` (keep paths in `app/config.py`
  so they can be patched).
- Secrets live in `.env` (gitignored); `.env.example` documents every variable.

## Verified API facts (checked 2026-10-29)

**mcp 2.3.0**
- `@server.tool()` returns the original function, so tests can call tools directly.
- `await server.call_tool(name, args)` returns a `CallToolResult`; use `.structured_content`
  to test through the MCP layer.

**higgsfield-client 0.2.0** (`import higgsfield_client`)
- Credentials: `HF_KEY` = `key_id:secret` (fallback: `HF_API_KEY` + `HF_API_SECRET`), read
  lazily by `higgsfield_client.auth.get_credential_key()`. Missing key raises
  `CredentialsMissedError` only when a call is made.
- Calls: `submit` / `subscribe` (+ `_async`), `status`, `result`, `cancel`, `upload*`.
  Base URL `https://api.higgsfield.ai`. Jobs are async: submit returns a `request_id`, then
  poll status or use a webhook. Output files are kept for at least 7 days, so download clips.
- Errors include `InsufficientCreditsError`; result statuses include `Failed` and `NSFW`.
- **No estimate helper in the SDK.** Call it with `httpx`:
  `POST https://api.higgsfield.ai/estimate/{model_path}`, headers
  `Authorization: Key {HF_KEY}` and `Content-Type: application/json`, body = the same JSON
  params as the generation call. Response: `{"usd": "0.094"}` (a string; parse with
  `Decimal`). The estimate for the authenticated account is authoritative.
  Source: https://docs.higgsfield.ai/docs/concepts/billing-and-retention
- Billing: only successful generations are charged; `failed`/`nsfw` are refunded; a queued
  request can be cancelled for a refund before processing starts.

**google-genai 2.34.0**
- `genai.Client()` reads `GOOGLE_API_KEY`, else `GEMINI_API_KEY` (the former wins if both set).
