"""runs/runs.csv: one row per generation attempt, for spend, the spend cap and latency."""

import csv
import threading
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

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
)
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


def path() -> Path:
    return config.RUNS_DIR / "runs.csv"


def rows() -> list[dict[str, str]]:
    if not path().exists():
        return []
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


def next_attempt(run_id: str, shot: int) -> int:
    return 1 + sum(1 for r in rows() if r["run_id"] == run_id and r["shot"] == str(shot))


def append(row: Row) -> None:
    *values, latency_s, cost_usd = astuple(row)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = [timestamp, *values, f"{latency_s:.1f}", f"{cost_usd:.3f}"]
    with _lock:
        is_new = not path().exists()
        path().parent.mkdir(parents=True, exist_ok=True)
        with path().open("a", newline="") as file:
            writer = csv.writer(file)
            if is_new:
                writer.writerow(FIELDS)
            writer.writerow(line)
