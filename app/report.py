"""runs/<run_id>/report.html: run totals, the brief, and one card per shot."""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

from pydantic import BaseModel

from app import clips, config, retrieval, run_log, shot_list


class BuiltReport(BaseModel):
    run_id: str
    report_path: str
    shots: int
    passed: int
    failed: int
    not_reviewed: int
    generations: int
    total_cost_usd: float


def build(run_id: str) -> BuiltReport:
    """Write the run's report.html from shots.json, runs.csv and the clips on disk."""
    run = shot_list.load(run_id)
    rows = [r for r in run_log.rows() if r["run_id"] == run_id and r["event"] == "generation"]
    attempts: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        attempts[int(row["shot"])].append(row)
    statuses = {s.index: _status(run, s.index, attempts[s.index]) for s in run.shots}
    total_cost = sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))
    counts = {v: sum(1 for s in statuses.values() if s == v) for v in ("pass", "fail", "none")}

    totals = [
        ("Total cost", f"${total_cost:.3f}"),
        ("Shots passed", f"{counts['pass']} of {len(run.shots)}"),
        ("Generations", str(len(rows))),
        ("Generation time", _duration(sum(float(r["latency_s"]) for r in rows))),
        ("Footage", f"{sum(s.duration_s for s in run.shots)} s"),
        ("Mode", " + ".join(sorted({r["mode"] for r in rows})) or "not generated"),
    ]
    cards = "\n".join(
        _card(run_id, run, shot, attempts[shot.index], statuses[shot.index]) for shot in run.shots
    )
    brief = next((b for b in retrieval.load_briefs() if b.id == run.brief_id), None)
    title = f"{brief.brand}: {brief.product}" if brief else f"Run {run_id}"
    built_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    tiles = "".join(f"<div><dt>{k}</dt><dd>{escape(v)}</dd></div>" for k, v in totals)
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>{escape(title)}</h1>
  <p class="muted">Run {escape(run_id)} · built {built_at}</p>
  <dl class="totals">{tiles}</dl>
  {_model_table(rows)}
</header>
{_brief(run.brief_id, brief)}
<nav aria-label="Filter shots">
  <button data-filter="all" aria-pressed="true">All</button>
  <button data-filter="pass" aria-pressed="false">Passed</button>
  <button data-filter="fail" aria-pressed="false">Failed</button>
  <button data-filter="none" aria-pressed="false">Not reviewed</button>
</nav>
<main>
{cards}
</main>
<script>{_JS}</script>
</body>
</html>
"""
    path = config.RUNS_DIR / run_id / "report.html"
    path.write_text(page)
    return BuiltReport(
        run_id=run_id,
        report_path=str(path),
        shots=len(run.shots),
        passed=counts["pass"],
        failed=counts["fail"],
        not_reviewed=counts["none"],
        generations=len(rows),
        total_cost_usd=float(total_cost),
    )


def _status(run: shot_list.RunShots, shot: int, attempts: list[dict[str, str]]) -> str:
    """The verdict on the clip on disk: a review of an earlier attempt doesn't describe it."""
    if not attempts:
        return "none"
    current = int(attempts[-1]["attempt"])
    reviews = [r for r in run.reviews if r.shot == shot and r.attempt == current]
    return reviews[-1].verdict if reviews else "none"


def _card(
    run_id: str,
    run: shot_list.RunShots,
    shot: shot_list.Shot,
    attempts: list[dict[str, str]],
    status: str,
) -> str:
    clip = clips.path_for(run_id, shot.index)
    poster = Path("frames") / f"shot_{shot.index}_start.jpg"
    if clip.is_file():
        poster_attr = f' poster="{poster}"' if (clip.parent / poster).is_file() else ""
        player = f'<video controls preload="metadata" playsinline src="{clip.name}"{poster_attr}>'
        player += "</video>"
    else:
        player = '<div class="no-clip">No clip yet</div>'
    reviews = [r for r in run.reviews if r.shot == shot.index]
    current = [r for r in reviews if attempts and r.attempt == int(attempts[-1]["attempt"])]
    if current:
        verdict = f'<p class="reasons">{escape(current[-1].reasons)}</p>'
    elif reviews:
        verdict = '<p class="reasons muted">This clip hasn\'t been reviewed yet.</p>'
    else:
        verdict = ""
    label = {"pass": "pass", "fail": "fail", "none": "not reviewed"}[status]
    spec = [
        ("Subject", shot.subject),
        ("Setting", shot.setting),
        ("Camera move", shot.camera_move),
        ("Lens", shot.lens),
        ("Lighting", shot.lighting),
        ("Mood", shot.mood),
        ("Duration", f"{shot.duration_s} s"),
        ("Start image", shot.image or "none (text to video)"),
    ]
    by_attempt = {r.attempt: r for r in reviews}
    attempt_rows = "".join(
        f"<tr><td>{escape(a['attempt'])}</td><td>{escape(a['mode'])}</td>"
        f"<td>{escape(a['latency_s'])} s</td><td>${escape(a['cost_usd'])}</td>"
        f"<td>{_review_cell(by_attempt.get(int(a['attempt'])))}</td></tr>"
        for a in attempts
    )
    cost = sum((Decimal(a["cost_usd"]) for a in attempts), Decimal(0))
    latency = sum(float(a["latency_s"]) for a in attempts)
    return f"""<article class="card" data-verdict="{status}">
  <div class="player">{player}</div>
  <div class="details">
    <h2>Shot {shot.index} <span class="badge {status}">{label}</span></h2>
    {verdict}
    <p><strong>{escape(shot.model)}</strong>: {escape(shot.model_reason)}</p>
    <dl class="spec">{"".join(f"<dt>{k}</dt><dd>{escape(v)}</dd>" for k, v in spec)}</dl>
    <details><summary>Prompt</summary><p>{escape(shot.prompt)}</p></details>
    <table>
      <caption>{len(attempts)} attempt(s) · ${cost:.3f} · {_duration(latency)}</caption>
      <thead><tr><th>#</th><th>Mode</th><th>Latency</th><th>Cost</th><th>QA</th></tr></thead>
      <tbody>{attempt_rows or '<tr><td colspan="5">Not generated</td></tr>'}</tbody>
    </table>
  </div>
</article>"""


def _review_cell(review: shot_list.Review | None) -> str:
    if review is None:
        return "—"
    title = escape(review.reasons)  # the reasons show on hover
    return f'<span class="badge {review.verdict}" title="{title}">{review.verdict}</span>'


def _model_table(rows: list[dict[str, str]]) -> str:
    by_model: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)
    if not by_model:
        return ""
    body = "".join(
        f"<tr><td>{escape(model)}</td><td>{len(group)}</td>"
        f"<td>${sum((Decimal(r['cost_usd']) for r in group), Decimal(0)):.3f}</td>"
        f"<td>{sum(float(r['latency_s']) for r in group) / len(group):.1f} s</td></tr>"
        for model, group in sorted(by_model.items())
    )
    return (
        '<table class="models"><caption>Per model</caption><thead><tr><th>Model</th>'
        "<th>Generations</th><th>Cost</th><th>Average latency</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _brief(brief_id: str, brief: retrieval.Brief | None) -> str:
    if brief is None:
        return (
            f'<section class="brief"><h2>Brief</h2><p class="muted">"{escape(brief_id)}" is '
            "not in data/briefs.json (a smoke-test run).</p></section>"
        )
    fields = [
        ("Brand", brief.brand),
        ("Product", brief.product),
        ("Audience", brief.audience),
        ("Goal", brief.goal),
        ("Tone", brief.tone),
        ("Key message", brief.key_message),
        ("Platform", brief.platform),
    ]
    must = "".join(f"<li>{escape(item)}</li>" for item in brief.must_include)
    rows = "".join(f"<dt>{k}</dt><dd>{escape(v)}</dd>" for k, v in fields)
    return (
        f'<section class="brief"><h2>Brief <span class="muted">{escape(brief.id)}</span></h2>'
        f'<dl class="spec">{rows}<dt>Must include</dt><dd><ul>{must}</ul></dd></dl></section>'
    )


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} min {seconds % 60:.0f} s"


_CSS = """
:root { --bg: #f6f6f4; --card: #fff; --text: #1d1d1b; --muted: #6b6b66; --line: #e3e3de;
  --pass: #1f7a3d; --fail: #b3261e; --none: #8a6d00; }
@media (prefers-color-scheme: dark) { :root { --bg: #141413; --card: #1f1f1d; --text: #ececea;
  --muted: #a3a39d; --line: #33332f; --pass: #5cc77f; --fail: #f2766d; --none: #e0c35c; } }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 24px 16px 48px; background: var(--bg);
  color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { margin: 0; font-size: 28px; } h2 { margin: 0 0 8px; font-size: 18px; }
.muted { color: var(--muted); }
.totals { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;
  margin: 20px 0; }
.totals div { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px; }
.totals dt { color: var(--muted); font-size: 13px; } .totals dd { margin: 0; font-size: 22px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; margin-top: 12px; }
caption { text-align: left; color: var(--muted); padding-bottom: 4px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); }
.brief, .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 16px; margin: 16px 0; }
.spec { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 8px 0; }
.spec dt { color: var(--muted); } .spec dd { margin: 0; }
.spec ul { margin: 0; padding-left: 18px; }
nav { display: flex; gap: 8px; flex-wrap: wrap; }
nav button { font: inherit; padding: 6px 14px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--text); }
nav button[aria-pressed="true"] { background: var(--text); color: var(--bg); }
.card { display: grid; grid-template-columns: minmax(200px, 300px) 1fr; gap: 20px; }
.card[hidden] { display: none; }
@media (max-width: 640px) { .card { grid-template-columns: 1fr; } }
video, .no-clip { width: 100%; aspect-ratio: 9 / 16; border-radius: 8px; background: #000; }
.no-clip { display: grid; place-items: center; color: #aaa; }
.badge { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  color: #fff; vertical-align: middle; }
.badge.pass { background: var(--pass); } .badge.fail { background: var(--fail); }
.badge.none { background: var(--none); }
.reasons { margin: 0 0 8px; }
details { margin-top: 8px; } summary { cursor: pointer; color: var(--muted); }
"""

_JS = """
const cards = document.querySelectorAll(".card");
const buttons = document.querySelectorAll("[data-filter]");
for (const button of buttons) {
  button.addEventListener("click", () => {
    const filter = button.dataset.filter;
    for (const b of buttons) b.setAttribute("aria-pressed", String(b === button));
    for (const card of cards) card.hidden = filter !== "all" && card.dataset.verdict !== filter;
  });
}
// One clip at a time: starting a video pauses the others.
for (const video of document.querySelectorAll("video")) {
  video.addEventListener("play", () => {
    for (const other of document.querySelectorAll("video")) if (other !== video) other.pause();
  });
}
"""
