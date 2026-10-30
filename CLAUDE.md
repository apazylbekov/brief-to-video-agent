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
./run.sh "<request>"         # the whole pipeline, headless (Claude spend capped at $2)
uv run python -m app.smoke "<prompt>"  # one 5 s draft clip, without the agent
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
  - `build_report`: writes `runs/<run_id>/report.html` (totals, brief, one card per shot). A
    card's verdict is for the clip on disk, so a review of an earlier attempt shows as "not
    reviewed". All agent-written text is HTML-escaped.

### Running it — `.mcp.json`, the skill, `run.sh`

- `.mcp.json` (project scope) starts the server as `brief-to-video` with
  `uv run --quiet python -m app.server`, passing `HF_KEY`, `GEMINI_API_KEY`, `VIDEO_MODE` and
  `MAX_SPEND_USD` as `${VAR:-}`. The empty default matters: an unset `${VAR}` with no default is
  passed as the literal text `${VAR}`. No secret is in the file.
- Precedence for those variables: non-empty shell value > `.env` > the code's default. A blank
  value counts as unset (`config.load_env`), so `.mcp.json`'s `""` still lets `.env` apply.
- The server starts in the directory `claude` is launched from, so launch it from the project
  root (`run.sh` does).
- `.claude/skills/brief-to-video/SKILL.md` is the agent's workflow (`/brief-to-video <request>`).
  It points to the Clip QA rubric below instead of repeating it.
- `run.sh` loads `.env` without overriding the shell, runs `claude -p "/brief-to-video …"`, saves
  Claude's JSON as `runs/<run_id>/agent.json`, appends `timestamp, run_id, total_cost_usd,
  duration_s, outcome` to `runs/agent_runs.csv`, and prints the report path.

### Video — Higgsfield API

- API docs: https://docs.higgsfield.ai/docs
- `app/config.py` defines two models from Higgsfield's catalog: a cheap **draft** model and a
  higher-quality one. Verify model IDs against the docs when choosing them.
- Call the cost-estimate endpoint before every generation.

### Modes and spend cap

- `VIDEO_MODE` controls video generation only. `mock` (default) costs $0: no calls to
  Higgsfield, no Higgsfield keys read. `live` calls the real APIs. Clip QA calls no API in
  either mode: the agent reviews the frames itself.
- Brief search ignores `VIDEO_MODE`: it uses Gemini embeddings (free tier) whenever
  `GEMINI_API_KEY` is set, keyword scores otherwise. Tests unset the key to get keywords.
- `MAX_SPEND_USD` (default 8) caps total Higgsfield spend. Before every generation (mock too,
  at mock prices), get the estimate and refuse if the total `cost_usd` in `runs/runs.csv`
  (all runs) plus estimates of clips still generating plus this estimate would exceed the cap.
- Mock `generate_clip` writes a 9:16 ffmpeg test-pattern clip and logs $0.
- `uv run python -m app.smoke "<prompt>"` generates one 5 s draft clip outside the agent; it
  saves a one-shot run via `shot_list.new_run`, bypassing the 3-4 shot rule for agent lists.

### Clip QA — the rubric the agent applies

QA is the agent's own judgement, not an API call. `review_clip(run_id, shot_index)` extracts
the start, middle and end frames to `runs/<run_id>/frames/` and returns their paths with the
shot spec. Open and look at all three frames, judge them against the spec with this rubric,
then call `record_review(run_id, shot_index, verdict, reasons)`.

A clip **passes** only if every check holds; one failed check makes it **fail**:

1. **Subject present:** the spec's `subject` is clearly visible and recognisable.
2. **Setting and lighting match:** the frames show the spec's `setting` and `lighting`
   (place, time of day, quality of light).
3. **Camera move plausible:** the change from start to middle to end fits `camera_move`
   (a dolly in gets closer, a pan sweeps sideways, `static` barely changes), with no jumps.
4. **No warped faces, hands or text:** no distorted faces, malformed hands or garbled
   lettering in any frame.

The verdict is `pass` or `fail`, with one line of reasons (300 characters at most). For a fail,
name each failed check and its frame, e.g. `hands warped in the middle frame; lighting is
midday, not sunrise`. For a pass, say briefly what matched.

- **Mock mode:** clips are ffmpeg test patterns (`mode` is `"mock"` in `review_clip`'s
  result), so the rubric can't apply. Record `pass` with reasons `mock placeholder`.
- **One regeneration:** a shot's first `fail` is regenerated once by `record_review` itself
  (same spec, within `MAX_SPEND_USD`); review the new clip the same way. A second `fail` is
  final. `next_step` in the result says what to do.
- `record_review` refuses a verdict unless `review_clip` ran on the shot's current clip.
- Verdicts go to `runs/runs.csv` (a `review` row) and to `reviews` in the run's `shots.json`.

### Run log — `runs/runs.csv`

Every generation and every review appends one row, including model, cost (USD) and latency,
so cost and latency can be reported per model. Columns: `timestamp, run_id, shot, attempt,
model, mode, seconds, latency_s, cost_usd, event, verdict, reasons`. Logs written before
reviews existed (the first nine columns) are upgraded in place, old rows marked `generation`.

- **`event = generation`:** `attempt` counts generation rows per run and shot, and a refused
  call adds no row. Live cost: the estimate once Higgsfield accepts the job; $0 if it ends
  failed/nsfw/canceled or the submit is rejected; the estimate if the outcome is unknown.
- **`event = review`:** `attempt` and `mode` are those of the shot's latest generation (the
  clip reviewed), `model` is its video model, `cost_usd` is 0, and `latency_s` runs from frame
  extraction to the verdict. Filter on `event` before averaging latency per model.

## Rules

- Type hints on every function, argument and return value, tests included (ruff `ANN` enforces).
- Every MCP tool has at least one pytest test.
- No test needs an API key. Tests run in mock mode, `monkeypatch.delenv` any keys they could
  pick up from `.env`, and point the runs directory at `tmp_path` (keep paths in `app/config.py`
  so they can be patched).
- Secrets live in `.env` (gitignored); `.env.example` documents every variable.
- CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check` and `pytest` on every
  push, with no secrets and ffmpeg installed via apt. Keep it that way: no test may need an API
  key or network access (fake the HTTP, as the live-path tests do).

## Verified API facts (checked 2026-10-29)

**mcp 2.3.0**
- `@server.tool()` returns the original function, so tests can call tools directly.
- `await server.call_tool(name, args)` returns a `CallToolResult`; use `.structured_content`
  to test through the MCP layer.
- A tool returning a Pydantic model gets `.structured_content` = that model's dict (not wrapped
  in `{"result": ...}`). Invalid arguments make `server.call_tool` raise `ToolError`.
- Only `ToolError` text reaches the client (as `is_error=True`, prefixed
  `Error executing tool <name>: `); any other exception is hidden as a crash.
- `SkipValidation[list[Model]]` on a tool parameter keeps the schema but passes the raw JSON to
  the function. `Field(min_length=...)` would still validate (and emit `minLength` for an
  array), so put `minItems`/`maxItems` in `json_schema_extra`.
- `mcp.Client(server)` connects in-process (tests); `(await client.list_tools()).tools`.
- Sync tool functions run in worker threads (`anyio.to_thread`), so tool calls can overlap:
  shared state (the spend check, `runs.csv` appends) needs a lock.

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
- Models in `app/config.py`: Kling 3.0 Standard (draft) and Pro (quality),
  `kling-video/v3.0/{std,pro}/{text,image}-to-video`. Both: integer `duration` 3-15, `sound`
  `"on"`/`"off"`, `cfg_scale`. Text-to-video adds `aspect_ratio` (`"16:9"`, `"9:16"`, `"1:1"`);
  image-to-video adds `image_url` (public URL of the first frame) and has no `aspect_ratio`.
  Result: `video.url`. The catalog isn't in the docs index; per-model pages live at
  `https://docs.higgsfield.ai/docs/models/<model>/<workflow>.md` (e.g. `kling-3/pro-text-to-video`).
- The estimate response has more than the docs show:
  `{"type": "estimate", "credits": "5.712", "usd": "0.357", "discount": {...}}`; `usd` is net
  of the discount. Prices are linear per second, rounded UP to $0.001. At 9:16 with sound off:
  Standard $0.0714/s, Pro $0.0952/s; sound on costs 50% more; image-to-video costs the same as
  text-to-video; Kling 3.0 Turbo has no `sound` field.
- SDK internals (read from the installed source): `submit(path, arguments=...)` returns a
  `SyncRequestController` (`.request_id`, `.status()`, `.get()`). `status()` returns a class
  (`Queued`, `InProgress`, `Completed`, `Failed`, `NSFW`, `Cancelled`; `DONE_STATUSES` is the
  finished ones) and drops the body; `get()` returns the final JSON (`video.url`, or `error`).
  `upload_file(path)` returns a `public_url` to pass as `image_url` (jpeg/png/webp/gif).
- The SDK retries 408/429/5xx 3 times within ~0.6 s, POSTs included, but not network errors
  (raw `httpx.TransportError`). Its errors are `HiggsfieldClientError` with the
  `httpx.HTTPStatusError` as `__cause__`. `submit()` takes no headers, so `app/clips.py` sets
  `Idempotency-Key` on the client's `_client` (the SDK's httpx session).
- `Idempotency-Key` (optional, 1-255 visible ASCII): the same key and body return the original
  `request_id` without a second charge; a different body gets 422. Retry an ambiguous submit
  with the same key. Docs: concepts/idempotency, concepts/errors.
- Retry with backoff: network errors, 408, 423, 429 (queue full), 500, 502-504. Don't retry
  400, 401, 403 (insufficient balance), 404, 422. Poll status from 2 s, x1.5 up to 10 s, with
  jitter (concepts/polling). Failed payload: `{"status": "failed", "error": "..."}`.
- First live generation (2026-10-30): Kling 3.0 Standard text-to-video, 9:16, 5 s, sound off
  gave 720x1280 H.264 at 24 fps, 5.04 s, no audio track, ~3.9 MB; 85.7 s from upload/submit to
  downloaded file; logged at the $0.357 estimate.
- First agent run, live (2026-10-30): Kling 3.0 Pro gives **1080x1920** (Standard 720x1280),
  both 24 fps, no audio. Latency per clip 86-129 s (Pro 105.6 s average over 4, Standard
  106.6 s). Generated text is unreliable: phone-screen and QR text came out garbled on both
  attempts, so asking the model for readable text, QR codes or prices fails rubric check 4.
- The estimate endpoint ignores unknown fields (returns 200), so it does not validate a body.
  Errors: `400 {"detail": "duration: 2 is less than the minimum of 3"}`,
  `404 {"detail": "model_not_found"}`.

**google-genai 2.34.0**
- `genai.Client()` reads `GOOGLE_API_KEY`, else `GEMINI_API_KEY` (the former wins if both set).
- Embeddings: `gemini-embedding-2` (GA, free tier, 3072 dims, unit-normalized). It ignores
  `task_type`; put the task in the text: `task: search result | query: {q}` for queries,
  `title: {t} | text: {body}` for documents. Its vectors are incompatible with
  `gemini-embedding-001`, so re-embed when switching.
- `embed_content(contents=[str, str, ...])` returns ONE combined embedding for
  `gemini-embedding-2`. For one vector per text, pass `[types.Content(parts=[types.Part(text=t)])
  for t in texts]`.

**python-dotenv**
- `load_dotenv()` with no argument searches up from the calling file, not the cwd; pass a path.
- `load_dotenv()` never overrides a variable that is set, even to `""`. `config.load_env` uses
  `dotenv_values()` instead, so blank counts as unset.

**Claude Code 2.1.312** (docs at code.claude.com, plus a probe server under `claude mcp list`)
- `.mcp.json` expands `${VAR}` and `${VAR:-default}` in `command`, `args`, `env`, `url`,
  `headers`. Verified: `${VAR:-}` (undocumented) gives `""`; an unset `${VAR}` gives the literal
  `${VAR}` plus a "Missing environment variables" warning; a variable set to `""` counts as set.
- A stdio server's cwd and `CLAUDE_PROJECT_DIR` are the directory `claude` was launched in,
  even if `.mcp.json` sits in a parent folder.
- `claude -p` loads project `.mcp.json` servers without approval and skips the trust dialog.
  `claude mcp list` doesn't connect unapproved ones; `claude --settings
  '{"enabledMcpjsonServers": ["brief-to-video"]}' mcp list` health-checks without a model call.
- `--allowedTools "mcp__brief-to-video__*"` allows every tool of that server. Read needs no
  approval inside the working directory, even in `-p`, and returns JPEGs as images.
- `claude -p "/skill-name args"` expands a project skill; `$ARGUMENTS` gets the args.
- `--max-budget-usd` is print-only and approximate ("spend can pass the cap"). The JSON result's
  fields follow the SDK's result message (`total_cost_usd`, `duration_ms`, `subtype`, ...), so
  `run.sh` reads them defensively.
