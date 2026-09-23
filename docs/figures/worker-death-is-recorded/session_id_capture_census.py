"""Count live runs whose pointer carries no session id, and name why.

Reads the real crew state: every run directory dispatched in the window, joined
to its live pointer, classified by the point at which session-id capture was
skipped. The classification is the point the capture path itself records on a
run it observes, applied here to pointers written before that path existed — so
the report and the code agree about what each absence means.

Read-only. Run it from the worktree with the project interpreter.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(
    "/home/ITER/mcintos/Code/.reckon-worktrees/reckon-c8f839407e49/"
    "ship-s19-20260922/every-dispatched-run-carries-a-session-id"
)
sys.path.insert(0, str(REPO))

from reckon.crew.resumption import resolve_session  # noqa: E402

RUNS = Path.home() / ".config/reckon/crew/runs"
LIVE = Path.home() / ".config/reckon/crew/live"
WINDOW_DAYS = 7

CAUSES = {
    "stream-not-folded": (
        "a session id is present in the run's own stream, so the pointer "
        "simply predates any observation that would record it"
    ),
    "harness-launch": (
        "the run is a task bound in the dispatching harness; it writes no "
        "backend stream, so no session id was ever captured for it"
    ),
    "stream-unreadable": (
        "cli launch whose recorded stream path is not a readable file, so "
        "capture had nothing to read"
    ),
    "stream-without-id": (
        "cli launch whose stream was read and carries no session id; the "
        "backend has not announced one for this run"
    ),
}


def classify(record: dict) -> str | None:
    """The point capture was skipped, or None when the pointer carries an id."""
    if record.get("session_id"):
        return None
    resolution = resolve_session(str(record.get("run_id") or ""), record=record)
    if resolution.get("session_id"):
        return "stream-not-folded"
    if str(record.get("launch") or "") != "cli":
        return "harness-launch"
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return "stream-unreadable"
    return "stream-without-id"


def main() -> int:
    now = time.time()
    cutoff = WINDOW_DAYS * 86400
    by_backend: Counter = Counter()
    by_point: Counter = Counter()
    by_pair: Counter = Counter()
    run_dirs = 0
    have_pointer = 0
    without = 0
    for name in sorted(os.listdir(RUNS)):
        path = RUNS / name
        if not path.is_dir():
            continue
        try:
            if now - path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        run_dirs += 1
        pointer = LIVE / f"{name}.json"
        if not pointer.is_file():
            continue
        have_pointer += 1
        try:
            record = json.loads(pointer.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        point = classify(record)
        if point is None:
            continue
        without += 1
        backend = str(record.get("backend") or "unknown")
        by_backend[backend] += 1
        by_point[point] += 1
        by_pair[f"{backend} / {point}"] += 1

    report = {
        "measured_at": datetime.datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
        "window_days": WINDOW_DAYS,
        "run_directories_in_window": run_dirs,
        "pointers_present": have_pointer,
        "pointers_without_session_id": without,
        "captured": have_pointer - without,
        "by_backend": dict(sorted(by_backend.items())),
        "by_point": dict(sorted(by_point.items())),
        "by_backend_and_point": dict(sorted(by_pair.items())),
        "point_causes": CAUSES,
    }
    out = REPO / "docs/research/data/session-id-capture.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"without": without, "of": have_pointer}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
