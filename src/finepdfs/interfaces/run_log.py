"""Observed wall-clock timing for one run, kept deliberately outside the declared record.

ADR-0006 promises that running twice changes nothing, and the coding standards say a published record
must be reproducible. A timestamp inside `out/run-summary.json` would rewrite declared target state on
every rerun and make two identical runs differ, so the counters the flow declares stay deterministic and
the observation lands here instead: beside the outputs, labelled observed, never declared target state.
The flow does not write this file -- the command wrapper that measures the run does -- and the browser
reads it when it is present and says "not recorded" when it is not.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The observed log's name, its owner's note, and the one place both the writer and the reader agree on
# where it lives: beside the declared outputs, so a browser given the output directory finds it.
OBSERVED_RUN_LOG = "last-run.json"
NOT_RECORDED = "not recorded"
OBSERVED_NOTE = (
    "observed wall-clock timing of one run; it is not declared target state and is not reproducible"
)


@dataclass(frozen=True, slots=True)
class ObservedRun:
    """What the wrapper around a run measured: when it started, ended, and how long it took."""

    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float

    def as_json(self) -> dict:
        return {
            "observed": True,
            "note": OBSERVED_NOTE,
            "started_at": _isoformat(self.started_at),
            "finished_at": _isoformat(self.finished_at),
            "elapsed_seconds": round(max(self.elapsed_seconds, 0.0), 3),
        }


class RunClock:
    """Measures one run's wall time. Owned by the command wrapper, never by the flow."""

    def __init__(self) -> None:
        self._started_wall = datetime.now(UTC)
        self._started = time.monotonic()

    def stop(self) -> ObservedRun:
        return ObservedRun(
            started_at=self._started_wall,
            finished_at=datetime.now(UTC),
            elapsed_seconds=max(time.monotonic() - self._started, 0.0),
        )


def observed_run_log_path(out_dir: Path | str) -> Path:
    """Where one output directory's observed timing lives; it is never a declared file."""
    return Path(out_dir) / OBSERVED_RUN_LOG


def write_observed_run(out_dir: Path | str, run: ObservedRun) -> Path:
    """Record the observation beside the outputs. Called by a command, never by the flow."""
    path = observed_run_log_path(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run.as_json(), indent=2) + "\n", encoding="utf-8")
    return path


def read_observed_run(out_dir: Path | str) -> dict | None:
    """The observed timing when one was recorded and labelled observed, otherwise ``None``."""
    path = observed_run_log_path(out_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("observed") is not True:
        return None
    return payload


def observed_elapsed(run: dict | None) -> float | None:
    """The elapsed seconds from a recorded observation, or ``None`` when it is not recorded."""
    if not run:
        return None
    elapsed = run.get("elapsed_seconds")
    return float(elapsed) if isinstance(elapsed, int | float) and not isinstance(elapsed, bool) else None


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
