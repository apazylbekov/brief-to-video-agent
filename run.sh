#!/usr/bin/env bash
# Run the brief-to-video agent headless, then print where its report is.
#
#   ./run.sh "a 15-second ad for a coffee shop in Almaty"
#   VIDEO_MODE=live ./run.sh "..."     # real Higgsfield clips, within MAX_SPEND_USD
#
# Claude's own spend is capped at $2. Its JSON output is saved as runs/<run_id>/agent.json, and
# its cost and duration are appended to runs/agent_runs.csv.
set -euo pipefail
cd "$(dirname "$0")"  # the MCP server starts in the directory claude is run from

if [[ $# -eq 0 ]]; then
  echo 'usage: ./run.sh "<one-line video request>"' >&2
  exit 2
fi

# Load .env without overriding variables already set (blank counts as unset), so
# VIDEO_MODE=live ./run.sh ... beats .env. .mcp.json passes these on to the server.
if [[ -f .env ]]; then
  while IFS='=' read -r key value || [[ -n $key ]]; do
    [[ $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue  # skips comments and blank lines
    if [[ -z ${!key:-} ]]; then
      value=${value%\"}
      value=${value#\"}
      export "$key=$value"
    fi
  done < .env
fi

mkdir -p runs
runs_before=$(ls runs)
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
output=$(mktemp)
trap 'rm -f "$output"' EXIT

status=0
SECONDS=0
claude -p "/brief-to-video $*" --allowedTools "mcp__brief-to-video__*" --output-format json \
  --max-budget-usd 2 > "$output" || status=$?
elapsed=$SECONDS

RUNS_BEFORE=$runs_before uv run --quiet python - "$output" "$started" "$elapsed" "$status" <<'PY'
"""Save Claude's JSON next to the run it made, log its cost and duration, print the report."""

import csv
import json
import os
import shutil
import sys
from pathlib import Path

output, started, elapsed, status = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
runs = Path("runs")
before = set(os.environ["RUNS_BEFORE"].split())
# The run this call made: a new folder with a shots.json (ids sort by time, so the last).
made = sorted(p.name for p in runs.iterdir() if p.name not in before and (p / "shots.json").is_file())
run_id = made[-1] if made else ""

try:
    result = json.loads(Path(output).read_text())
except (OSError, ValueError):
    result = {}  # claude failed before printing its JSON
saved = runs / run_id / "agent.json" if run_id else runs / f"agent-{started.replace(':', '')}.json"
shutil.copyfile(output, saved)

cost = result.get("total_cost_usd")
duration_s = round(result["duration_ms"] / 1000, 1) if "duration_ms" in result else elapsed
outcome = result.get("subtype") or ("no output" if not result else "unknown")
log = runs / "agent_runs.csv"
is_new = not log.exists()
with log.open("a", newline="") as file:
    writer = csv.writer(file)
    if is_new:
        writer.writerow(["timestamp", "run_id", "total_cost_usd", "duration_s", "outcome"])
    writer.writerow([started, run_id, "" if cost is None else cost, duration_s, outcome])

claude_cost = "unknown" if cost is None else f"${cost:.4f}"
print(f"Claude: {claude_cost} in {duration_s:.0f} s ({outcome}); output saved to {saved}")
report = runs / run_id / "report.html"
if run_id and report.is_file():
    print(f"Report: {report.resolve()}")
else:
    print(f"No report: the agent didn't reach build_report. See {saved}.")
sys.exit(status)
PY
