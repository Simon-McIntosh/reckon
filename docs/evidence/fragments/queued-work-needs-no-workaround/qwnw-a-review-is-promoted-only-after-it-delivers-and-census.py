"""Census: every run id the last day's admission log and dispatch records name
still has a live pointer, a ledger row, or a discard/failure record.

Run it with one argument, the path to write the per-id output to; the control
block is printed as JSON on stdout, so the two streams are captured separately:

    /home/ITER/mcintos/Code/reckon/.venv/bin/python <this file> <output.json> > <summary.json>

The window is the twenty-four hours ending when the census starts, so a re-run
reports on its own day rather than reproducing the recorded one. The recorded
run's full output is saved beside this file as
qwnw-a-review-is-promoted-only-after-it-delivers-and-census.json; its window
and every count the landing record quotes are in that file's ``summary`` block.

The run id used for the self-control is read from CENSUS_CONTROL_RUN. The
recorded run had no ledger row when the census ran, because a run is ledgered
only on promotion, so its own ledger control is false by construction and the
census reports a sampled ledger id instead: one whose per-run ledger file
exists, which proves the reader resolves rather than reporting a false negative
for the whole surface.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
CREW = HOME / ".config/reckon/crew"
WINDOW = timedelta(hours=24)
RUN_STAMP = re.compile(r"^r-(\d{8}T\d{6})(\d{6})")

now = datetime.now(timezone.utc)
start = now - WINDOW


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _collect_run_ids(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run_id" and isinstance(value, str) and value.strip():
                found.add(value.strip())
            else:
                _collect_run_ids(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_run_ids(item, found)


verdict_ids = set()
verdict_lines = 0
verdict_lines_in_window = 0
bad_lines = 0
log = CREW / "admission-verdicts.jsonl"
for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    verdict_lines += 1
    try:
        record = json.loads(line)
    except ValueError:
        bad_lines += 1
        continue
    stamp = _parse(record.get("at") or record.get("started_at"))
    if stamp is None or stamp < start:
        continue
    verdict_lines_in_window += 1
    _collect_run_ids(record, verdict_ids)

dispatch_ids = set()
dispatch_detail_missing = 0
runs_dir = CREW / "runs"
for name in sorted(os.listdir(runs_dir)):
    match = RUN_STAMP.match(name)
    if not match:
        continue
    minted = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
        tzinfo=timezone.utc
    )
    if minted < start or minted > now:
        continue
    detail = runs_dir / name / "dispatch-detail.json"
    if not detail.is_file():
        dispatch_detail_missing += 1
        continue
    try:
        detail_stamp = datetime.fromtimestamp(detail.stat().st_mtime, timezone.utc)
    except OSError:
        detail_stamp = None
    # A run minted inside the window whose dispatch record was written long
    # through the window belongs to the window; one whose record predates it by
    # more than the window itself is from an earlier day's dispatch.
    if detail_stamp is not None and detail_stamp < start - WINDOW:
        continue
    dispatch_ids.add(name)

ledger_ids = set()
mounts = json.loads((HOME / ".config/reckon/mounts.json").read_text())
ledger_files = 0
ledger_dirs = 0
for project, docs in mounts.items():
    directory = Path(docs) / "state" / project / "runs"
    try:
        names = os.listdir(directory)
    except OSError:
        continue
    ledger_dirs += 1
    ledger_files += len(names)
    for name in names:
        if name.endswith(".json"):
            ledger_ids.add(name[: -len(".json")])

discard_dir = CREW / "discards"
archive_runs = CREW / "archive" / "runs"
reports_dir = CREW / "reports"
FAILURE_PHASES = {
    "failed",
    "launch-failed",
    "ended-without-manifest",
    "interrupted",
    "discarded",
}


def provenance(run_id):
    surfaces = []
    pointer = CREW / "live" / f"{run_id}.json"
    if pointer.is_file():
        surfaces.append("live-pointer")
        try:
            body = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            body = {}
        phase = str(body.get("phase") or "")
        if phase in FAILURE_PHASES or body.get("launch_failures"):
            surfaces.append("failure-record")
    if run_id in ledger_ids:
        surfaces.append("ledger-row")
    run_dir = runs_dir / run_id
    archived = archive_runs / run_id
    if (
        (run_dir / "discard.json").is_file()
        or (discard_dir / f"{run_id}.json").is_file()
        or (archived / "discard.json").is_file()
    ):
        surfaces.append("discard-record")
    if run_dir.is_dir():
        for attempt in sorted(run_dir.glob("attempt-*-exit.json")):
            text = attempt.read_text(encoding="utf-8", errors="replace")
            if '"exit_status": 0' not in text.replace(" ", ""):
                surfaces.append("failure-record")
                break
    refusal = CREW / "refusal-traces" / f"launch-{run_id}.json"
    if refusal.is_file():
        surfaces.append("failure-record")
    end = archived / "end.json"
    if end.is_file():
        try:
            kind = str(json.loads(end.read_text(encoding="utf-8")).get("kind") or "")
        except (OSError, ValueError):
            kind = ""
        if kind == "discarded":
            surfaces.append("discard-record")
        elif kind:
            surfaces.append("failure-record")
    if (archived / "classification.json").is_file():
        surfaces.append("archived-run-record")
    # A composed plan review is not a crew run; its dispatch is recorded in its
    # report directory, whose sidecar names the crew run that ran it.
    if "-plan-review-of-" in run_id:
        for report in reports_dir.glob(f"*/plan-review/*/{run_id}"):
            if (report / "dispatch.json").is_file():
                surfaces.append("plan-review-report")
                break
    return sorted(set(surfaces))


named = sorted(verdict_ids | dispatch_ids)
CORE = ("live-pointer", "ledger-row", "discard-record", "failure-record")
vanished = [
    run_id for run_id in named if not set(provenance(run_id)) & set(CORE)
]

MY_RUN = os.environ.get("CENSUS_CONTROL_RUN", "")
control = {
    "verdict_log_lines": verdict_lines,
    "verdict_log_lines_in_window": verdict_lines_in_window,
    "verdict_log_unparseable_lines": bad_lines,
    "window_start": start.isoformat(),
    "window_end": now.isoformat(),
    "run_ids_from_verdict_log": len(verdict_ids),
    "run_ids_from_dispatch_records": len(dispatch_ids),
    "run_ids_from_both": len(verdict_ids & dispatch_ids),
    "dispatch_records_without_detail": dispatch_detail_missing,
    "ledger_files_read": ledger_files,
    "ledger_run_ids": len(ledger_ids),
    "named_total": len(named),
    "vanished": vanished,
    "positive_control_known_live_pointer": "live-pointer" in provenance(MY_RUN),
    "positive_control_known_ledger_row": MY_RUN in ledger_ids,
    "positive_control_sampled_ledger_row": next(
        (
            {"run_id": run_id, "surfaces": provenance(run_id)}
            for run_id in named
            if "ledger-row" in provenance(run_id)
        ),
        None,
    ),
    "surfaces": {
        key: sum(1 for run_id in named if key in provenance(run_id))
        for key in (
            "live-pointer",
            "ledger-row",
            "discard-record",
            "failure-record",
            "archived-run-record",
            "plan-review-report",
        )
    },
    "positive_control_archived_discard": provenance(
        "r-20261114T174611550929-sef-s5"
    ),
    "positive_control_launch_refusal": provenance(
        "r-20261114T200118732970-review-of-bank-hessian-diagnosis-6b4726c6b116-figure"
    ),
}
print(json.dumps(control, indent=2))
Path(sys.argv[1]).write_text(
    json.dumps(
        {"summary": control, "rows": {run_id: provenance(run_id) for run_id in named}},
        indent=1,
    )
    + "\n",
    encoding="utf-8",
)