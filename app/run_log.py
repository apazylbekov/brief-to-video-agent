"""runs/runs.csv: one row per generation attempt and per review, for spend, the cap and latency."""

import csv
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from app import config
from app.errors import ReportableError

FIELDS = (
    "timestamp",
    "run_id",
    "shot",
    "attempt",
    "model",
    "mode",
    "seconds",
    "latency_s",
    "cost_usd",
    "event",
    "verdict",
    "reasons",
)
_BEFORE_REVIEWS = FIELDS[:9]  # logs written before reviews had the first nine columns only
_lock = threading.Lock()  # MCPServer runs sync tools in worker threads


@dataclass(frozen=True)
class Row:
    run_id: str
    shot: int
    attempt: int
    model: str
    mode: str
    seconds: int
    latency_s: float
    cost_usd: Decimal
    event: Literal["generation", "review"] = "generation"
    verdict: str = ""
    reasons: str = ""


def path() -> Path:
    return config.RUNS_DIR / "runs.csv"


def rows() -> list[dict[str, str]]:
    with _lock:
        if not path().exists():
            return []
        _upgrade()
        with path().open(newline="") as file:
            return list(csv.DictReader(file))


def total_spend() -> Decimal:
    total = Decimal(0)
    for line, row in enumerate(rows(), start=2):  # line 1 is the header
        try:
            total += Decimal(row["cost_usd"])
        except (InvalidOperation, KeyError, TypeError) as exc:
            raise ReportableError(
                f"{path()} line {line}: cost_usd {row.get('cost_usd')!r} is not a number. "
                "Fix the file before generating more clips: the spend cap depends on it."
            ) from exc
    return total


def generations(run_id: str, shot: int) -> list[dict[str, str]]:
    return [
        r
        for r in rows()
        if r["event"] == "generation" and r["run_id"] == run_id and r["shot"] == str(shot)
    ]


def next_attempt(run_id: str, shot: int) -> int:
    return len(generations(run_id, shot)) + 1


def append(row: Row) -> None:
    line = [
        datetime.now(UTC).isoformat(timespec="seconds"),
        row.run_id,
        row.shot,
        row.attempt,
        row.model,
        row.mode,
        row.seconds,
        f"{row.latency_s:.1f}",
        f"{row.cost_usd:.3f}",
        row.event,
        row.verdict,
        row.reasons,
    ]
    with _lock:
        is_new = not path().exists()
        if not is_new:
            _upgrade()
        path().parent.mkdir(parents=True, exist_ok=True)
        with path().open("a", newline="") as file:
            writer = csv.writer(file)
            if is_new:
                writer.writerow(FIELDS)
            writer.writerow(line)


def _upgrade() -> None:
    """Add the review columns to a log written before reviews existed. Caller holds _lock."""
    with path().open(newline="") as file:
        reader = csv.reader(file)
        header = tuple(next(reader, ()))
        body = list(reader)
    if header == FIELDS:
        return
    if header != _BEFORE_REVIEWS:
        raise ReportableError(f"{path()} has unexpected columns {list(header)}; expected {FIELDS}")
    partial = path().with_suffix(".csv.part")
    with partial.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(FIELDS)
        writer.writerows([*line, "generation", "", ""] for line in body)
    partial.replace(path())
