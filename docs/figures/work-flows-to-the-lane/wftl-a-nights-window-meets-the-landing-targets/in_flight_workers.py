"""The working-worker mean with runs still in flight at the window's end counted.

The lane census builds a worker's span from its launch and its exit, falling back to
dispatch and completion. A run whose worker is still alive when the census runs
has neither an exit nor a completion, so the census leaves it out of the mean
entirely, however long it had been working inside the window. That undercount
is invisible in a window measured long after it closed and material in one
measured while its workers are still running.

This reads the same ledger through the census's own loader and reports both
figures: the census's mean, and the mean with every run dispatched before the
window's end and not finished by it counted as working until the window's end
(or its eventual finish, if that is recorded). An in-flight run is one the
store holds without a completion whose dispatch precedes the window's end;
nothing is inferred about a row the store does not carry.

    python in_flight_workers.py --window 2026-11-17T00:00Z/2026-11-17T04:00Z
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import lane_window_census as census  # noqa: E402


def unstamped(run: dict) -> bool:
    return not run.get("completed_at") and not run.get("exited_at")


def latest_stamp(run: dict) -> dt.datetime | None:
    for key in ("exited_at", "completed_at"):
        text = run.get(key)
        if not text:
            continue
        try:
            return census.parse_instant(text)
        except ValueError:
            continue
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", required=True, help="START/END, ISO instants")
    args = parser.parse_args(argv)
    start, end = census.parse_window(args.window)
    runs, counts = census.load_runs()
    spans = census.worker_spans(runs)
    base = census.concurrency(spans, start, end)

    inherited = 0.0
    in_flight = []
    for run in runs:
        dispatched = run.get("dispatched_at")
        if not dispatched:
            continue
        try:
            first = census.parse_instant(dispatched)
        except ValueError:
            continue
        if first >= end:
            continue
        last = latest_stamp(run)
        if last is not None and last <= end:
            continue
        counted = any(span[2] is run for span in spans)
        if counted and last is None:
            continue
        stop = min(last or end, end)
        seconds = max(0.0, (stop - max(first, start)).total_seconds())
        if seconds <= 0:
            continue
        in_flight.append(
            {
                "run_id": run["run_id"],
                "role": run["role"],
                "dispatched_at": dispatched,
                "stopped_at": (last or end).isoformat(),
                "seconds_in_window": round(seconds),
            }
        )
        inherited += seconds

    window_seconds = (end - start).total_seconds()
    print(
        json.dumps(
            {
                "window": [start.isoformat(), end.isoformat()],
                "read": counts,
                "census_mean": base,
                "in_flight_runs": len(in_flight),
                "in_flight_seconds": round(inherited),
                "corrected_mean": round(
                    (base["mean"] * window_seconds + inherited) / window_seconds, 2
                ),
                "in_flight": in_flight,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
