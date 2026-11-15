"""Census of the crew lane over a closed window.

Reads three instruments - the run ledger, the per-run landing records and the
admission verdict log - and reports, for each window it is given, how many
workers were working, how long a completed implement run waited to be
promoted, whether an admit pass held a request on a claim under a shared root,
and how many worker identity files the claims directory held.

    python scripts/lane_window_census.py --window 2026-11-14T21:00Z/2026-11-15T01:00Z
    python scripts/lane_window_census.py --baseline
    python scripts/lane_window_census.py --negative-control

Every figure is reported beside the population it was computed over, so an
empty result reads as empty rather than as a clean number.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path

CREW_HOME = Path(
    os.environ.get("RECKON_CREW_HOME", "~/.config/reckon/crew")
).expanduser()
RUN_STORE = CREW_HOME / "run_store.db"
ADMISSION_LOG = CREW_HOME / "admission-verdicts.jsonl"
CLAIMS_DIR = CREW_HOME / "claims"
RUN_DIRS = (CREW_HOME / "runs", CREW_HOME / "archive" / "runs")

WINDOW = ("2026-11-14T21:00:00+00:00", "2026-11-15T01:00:00+00:00")
BASELINE = ("2026-11-07T00:00:00+00:00", "2026-11-12T00:00:00+00:00")
NEGATIVE_CONTROL = ("2026-11-11T13:00:00+00:00", "2026-11-11T15:00:00+00:00")


IDENTITY_FILE = re.compile(r"^exec-\d+\.identity$")


def parse_instant(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def parse_window(text: str) -> tuple[dt.datetime, dt.datetime]:
    start, _, end = text.partition("/")
    return parse_instant(start), parse_instant(end)


def role_of(payload: dict, node: str) -> str:
    role = payload.get("role")
    if role:
        return role
    return (
        "review"
        if str(node).startswith(("review-of", "plan-review-of"))
        else "implement"
    )


def load_runs() -> list[dict]:
    if not RUN_STORE.exists():
        raise SystemExit(f"the run ledger is not at {RUN_STORE}")
    con = sqlite3.connect(f"file:{RUN_STORE}?mode=ro", uri=True)
    runs = []
    for run_id, project, node, completed, payload in con.execute(
        "select run_id, project, node, completed_at, payload from runs"
    ):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue
        worker = data.get("worker_exit") or {}
        runs.append(
            {
                "run_id": run_id,
                "project": project,
                "node": node,
                "role": role_of(data, node),
                "dispatched_at": data.get("dispatched_at"),
                "completed_at": completed,
                "launched_at": worker.get("launched_at"),
                "exited_at": worker.get("exited_at"),
            }
        )
    con.close()
    return runs


def worker_spans(runs: list[dict]) -> list[tuple[dt.datetime, dt.datetime, dict]]:
    """One span per run: its worker process alive, else its dispatch to completion.

    The worker's own launch and exit bound the time it could have been
    working. The ledger's dispatch and completion stamps bracket a wider
    interval that also carries queueing and promotion, so they are only the
    fallback for a run whose worker exit was never recorded.
    """
    spans = []
    for run in runs:
        start, end = run["launched_at"], run["exited_at"]
        try:
            if not (start and end):
                start, end = run["dispatched_at"], run["completed_at"]
            if not (start and end):
                continue
            first, last = parse_instant(start), parse_instant(end)
        except (TypeError, ValueError):
            continue
        if last > first:
            spans.append((first, last, run))
    spans.sort(key=lambda span: span[0])
    return spans


def concurrency(spans, start: dt.datetime, end: dt.datetime) -> dict:
    """Time-weighted mean and peak of overlapping spans inside the window."""
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise SystemExit("the window ends before it starts")
    events = []
    involved = 0
    for first, last, _run in spans:
        if last <= start or first >= end:
            continue
        involved += 1
        events.append((max(first, start), 1))
        events.append((min(last, end), -1))
    events.sort(key=lambda pair: (pair[0], pair[1]))
    area = 0.0
    live = 0
    peak = 0
    previous = start
    for when, delta in events:
        area += live * (when - previous).total_seconds()
        previous = when
        live += delta
        peak = max(peak, live)
    return {"mean": round(area / seconds, 2), "peak": peak, "workers": involved}


def landing_stamps() -> dict[str, dt.datetime]:
    """The time each run's landing was committed, from its landing record."""
    stamps: dict[str, dt.datetime] = {}
    for base in RUN_DIRS:
        if not base.is_dir():
            continue
        for name in os.listdir(base):
            path = base / name / "landing.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            record = data[0] if isinstance(data, list) and data else data
            if not isinstance(record, dict) or not record.get("time"):
                continue
            try:
                stamps.setdefault(name, parse_instant(record["time"]))
            except ValueError:
                continue
    return stamps


def promotion_latency(
    runs: list[dict],
    stamps: dict[str, dt.datetime],
    start: dt.datetime,
    end: dt.datetime,
) -> dict:
    """Completion-to-promotion seconds for the implement runs completed in the window.

    The cohort is the implement runs that completed inside the window, so two
    windows compare the same population. A landing record survives only while
    its run directory survives, so a run whose directory was reclaimed reports
    as having no record; that count is reported beside the gaps rather than
    folded into them.
    """
    gaps = []
    clamped = 0
    cohort = 0
    absent = 0
    for run in runs:
        if run["role"] != "implement":
            continue
        completed = run["completed_at"]
        if not completed:
            continue
        try:
            moment = parse_instant(completed)
        except ValueError:
            continue
        if not (start <= moment < end):
            continue
        cohort += 1
        promoted = stamps.get(run["run_id"])
        if not promoted:
            absent += 1
            continue
        gap = (promoted - moment).total_seconds()
        if gap < 0:
            clamped += 1
            gap = 0.0
        gaps.append(gap)
    gaps.sort()
    if not gaps:
        return {
            "cohort": cohort,
            "n": 0,
            "no_landing_record": absent,
            "median_seconds": None,
            "p90_seconds": None,
            "clamped_negative": clamped,
        }
    p90 = gaps[min(len(gaps) - 1, round(0.9 * (len(gaps) - 1)))]
    return {
        "cohort": cohort,
        "n": len(gaps),
        "no_landing_record": absent,
        "median_seconds": round(statistics.median(gaps), 1),
        "p90_seconds": round(p90, 1),
        "clamped_negative": clamped,
    }


def shared_root(path_text: str) -> bool:
    """Whether a claimed path sits under the crew home every node shares."""
    if not path_text:
        return False
    path = Path(path_text).expanduser()
    return str(path).startswith(str(CREW_HOME) + os.sep)


def claimed_path(detail: str) -> str:
    """The path an admission hold's detail quotes, if it quotes one."""
    quoted = detail.split("'")
    return quoted[3] if len(quoted) > 3 else ""


def admission_span() -> tuple[dt.datetime, dt.datetime] | None:
    """The instants the admission log's first and last pass were written at."""
    if not ADMISSION_LOG.exists():
        return None
    stamps = []
    for line in ADMISSION_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            stamps.append(parse_instant(json.loads(line)["at"]))
        except (ValueError, KeyError, TypeError):
            continue
    if not stamps:
        return None
    return min(stamps), max(stamps)


def admission_holds(start: dt.datetime, end: dt.datetime, span) -> dict:
    """Admit passes in the window, and those holding a request on a shared-root claim.

    The log is capped to the passes it still holds, so a window the log does
    not cover reports that it was not measured rather than reporting zero.
    """
    if span is None:
        return {
            "passes": None,
            "passes_holding_shared_claim": None,
            "shared_claim_pointers": None,
            "note": f"the admission log is not at {ADMISSION_LOG}",
        }
    if end <= span[0] or start >= span[1]:
        return {
            "passes": None,
            "passes_holding_shared_claim": None,
            "shared_claim_pointers": None,
            "note": "the window lies outside the passes the admission log still holds",
            "log_span": [span[0].isoformat(), span[1].isoformat()],
        }
    passes = 0
    holding = 0
    pointers = 0
    for line in ADMISSION_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            when = parse_instant(row["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (start <= when < end):
            continue
        passes += 1
        shared = sum(
            1
            for pointer in row.get("pointers") or []
            if pointer.get("verdict") == "held-on-claim"
            and shared_root(claimed_path(pointer.get("detail") or ""))
        )
        pointers += shared
        if shared:
            holding += 1
    return {
        "passes": passes,
        "passes_holding_shared_claim": holding,
        "shared_claim_pointers": pointers,
    }


def identity_files(as_of: dt.datetime) -> dict:
    """Worker identity files in the claims directory at an instant, with its total entries.

    An identity file is written once, when its process first publishes it, so a
    file's modification time places it at the moment it appeared. Counting by
    that stamp reports the directory as it stood at the window's end rather
    than as it stands when this census runs.
    """
    if not CLAIMS_DIR.is_dir():
        return {
            "identity_files": None,
            "claims_entries": None,
            "as_of": as_of.isoformat(),
        }
    names = os.listdir(CLAIMS_DIR)
    identities = 0
    entries = 0
    for name in names:
        try:
            stamp = (CLAIMS_DIR / name).stat().st_mtime
        except OSError:
            continue
        if stamp > as_of.timestamp():
            continue
        entries += 1
        if IDENTITY_FILE.match(name):
            identities += 1
    return {
        "identity_files": identities,
        "claims_entries": entries,
        "as_of": as_of.isoformat(),
    }


def dispatches(runs: list[dict], start: dt.datetime, end: dt.datetime) -> dict:
    """Requests dispatched in the window, and the distinct plan sections they name."""
    count = 0
    sections = set()
    for run in runs:
        stamp = run["dispatched_at"]
        if not stamp:
            continue
        try:
            moment = parse_instant(stamp)
        except ValueError:
            continue
        if start <= moment < end:
            count += 1
            sections.add((run["project"], run["node"]))
    return {"dispatched": count, "distinct_project_nodes": len(sections)}


def census(start: dt.datetime, end: dt.datetime, runs, spans, stamps, span) -> dict:
    return {
        "window": [start.isoformat(), end.isoformat()],
        "dispatches": dispatches(runs, start, end),
        "working_workers": concurrency(spans, start, end),
        "completion_to_promotion": promotion_latency(runs, stamps, start, end),
        "admit_holds": admission_holds(start, end, span),
        "identity_files": identity_files(end),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window", help="START/END, for example 2026-11-14T21:00Z/2026-11-15T01:00Z"
    )
    parser.add_argument(
        "--baseline", action="store_true", help="report the 7 to 11 November baseline"
    )
    parser.add_argument(
        "--negative-control",
        action="store_true",
        help="report 11 November 13:00 to 15:00 UTC, which must read below 12 working workers",
    )
    args = parser.parse_args(argv)

    chosen = []
    if args.window:
        chosen.append(("window", parse_window(args.window)))
    if args.baseline:
        chosen.append(("baseline", parse_window("/".join(BASELINE))))
    if args.negative_control:
        chosen.append(("negative_controls", parse_window("/".join(NEGATIVE_CONTROL))))
    if not chosen:
        chosen.append(("window", parse_window("/".join(WINDOW))))

    runs = load_runs()
    spans = worker_spans(runs)
    stamps = landing_stamps()
    span = admission_span()
    report = {
        "read": {
            "run_store": str(RUN_STORE),
            "admission_log": str(ADMISSION_LOG),
            "claims_dir": str(CLAIMS_DIR),
            "landing_records": len(stamps),
            "runs_in_ledger": len(runs),
            "admission_log_span": [span[0].isoformat(), span[1].isoformat()]
            if span
            else None,
            "worker_spans": len(spans),
        },
        "run_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    for name, (start, end) in chosen:
        report[name] = census(start, end, runs, spans, stamps, span)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
