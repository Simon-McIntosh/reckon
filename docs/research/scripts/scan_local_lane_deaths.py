#!/usr/bin/env python3
"""Scan the local lane's own run streams for mid-turn deaths.

Every run directory under the crew runs root carries streams named
``stream.jsonl`` (first attempt) and ``resume-N.jsonl`` (later attempts). A
harness that reached the end of a turn emitted a top-level ``{"type":"result"}``
record; a process killed mid-stream emitted none and its stream simply stops.
That single bit is the classification, read from the stream itself rather than
from any rollup.

Two views are reported and they are not the same number:

* ``attempt`` — every attempt stream is one observation. A death here is the
  event the plan counts ("died mid-turn nine times in one day").
* ``terminal`` — only the run's last attempt is read, so a run that died and
  was resumed to completion reads as completed. This is the convention
  ``reckon.crew.death_census`` documents.

The headline death count is the review role **at the lane's declared review
effort**. A review attempt dispatched at any other effort is counted in the
role-wide figure beside it. They are stated separately because they are
different populations, and because a headline that silently pooled them would
not equal the effort cell a reader checks it against.

Usage (bounded by the caller; the corpus parses at roughly 55 MB/s):

    timeout 1200 <venv>/bin/python docs/research/scripts/scan_local_lane_deaths.py \
        --out docs/research/data/local-lane-deaths.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

CREW_HOME = Path.home() / ".config/reckon" / "crew"
RUNS_ROOT = CREW_HOME / "runs"
LIVE_DIR = CREW_HOME / "live"
LOCAL_BACKEND = "clive"
# The effort the review reflex is dispatched at on this lane. The headline
# count is the review role at this effort, so the figure a reader checks
# against the effort cell is the figure the cell holds.
REVIEW_EFFORT = "xhigh"
# Where this script is committed, recorded in the output so the file names the
# revision-controlled script that produced it.
MEASUREMENT_SCRIPT = "docs/research/scripts/scan_local_lane_deaths.py"

# A run whose first attempt the supervisor's own exit record calls a signal
# death while its stream ends with no result record, named here so the
# classification is checked against a run known to have died rather than
# against the counts the same parser produced.
CONTROL_RUN_ID = (
    "r-20260928T044354744785-review-of-reflex-keys-on-reviewed-run-and-head"
)

_TS_RE = re.compile(rb'"timestamp":"([0-9T:.Z+-]+)"')
_TS_SCAN = 262_144


def attempt_streams(run_dir: Path) -> list[tuple[str, Path]]:
    """Ordered (label, path) for a run's own attempt streams."""
    out: list[tuple[str, Path]] = []
    initial = run_dir / "stream.jsonl"
    if initial.is_file():
        out.append(("stream", initial))
    resumes: list[tuple[int, Path]] = []
    for entry in sorted(run_dir.iterdir()):
        name = entry.name
        if name.startswith("resume-") and name.endswith(".jsonl"):
            stem = name[len("resume-") : -len(".jsonl")]
            if stem.isdigit():
                resumes.append((int(stem), entry))
    out.extend((f"resume-{rank}", path) for rank, path in sorted(resumes))
    return out


def stream_facts(path: Path) -> dict:
    """Terminal shape of one stream, read without trusting record content.

    Every line is parsed and the record's own top-level ``type`` is read. A
    pattern match on the line's opening bytes is not enough: the harness writes
    the ``result`` record with ``duration_api_ms`` first, so a reader anchored
    on ``{"type"`` never sees the one record that separates a completion from a
    death.
    """
    facts = {
        "record_count": 0,
        "parse_failures": 0,
        "has_result_record": False,
        "last_record_type": None,
        "last_record_subtype": None,
    }
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        facts["unreadable"] = True
        return facts
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except (TypeError, ValueError):
                facts["parse_failures"] += 1
                continue
            if not isinstance(event, dict):
                facts["parse_failures"] += 1
                continue
            facts["record_count"] += 1
            record_type = event.get("type")
            if record_type == "result":
                facts["has_result_record"] = True
            facts["last_record_type"] = record_type
            subtype = event.get("subtype")
            facts["last_record_subtype"] = subtype if isinstance(subtype, str) else None
    return facts


def stream_times(path: Path) -> dict:
    """First and last ISO timestamps in a stream, from a bounded head/tail read.

    A stream carrying timestamps in its records answers when the attempt ran
    without parsing records; a stream that carries none falls back to the
    file's own mtime and says which source answered.
    """
    size = path.stat().st_size
    stamps: list[str] = []
    try:
        with path.open("rb") as handle:
            head = handle.read(_TS_SCAN)
            stamps.extend(match.decode() for match in _TS_RE.findall(head))
            if size > _TS_SCAN:
                handle.seek(max(0, size - _TS_SCAN))
                tail = handle.read(_TS_SCAN)
                stamps.extend(match.decode() for match in _TS_RE.findall(tail))
    except OSError:
        stamps = []
    if stamps:
        return {"first": min(stamps), "last": max(stamps), "source": "stream"}
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    stamp = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"first": stamp, "last": stamp, "source": "mtime"}


def classify_attempt(
    facts: dict, *, alive: bool | None = None, terminal: bool = True
) -> str:
    """Name one attempt's outcome from its stream shape.

    This is the one classifier both the corpus and the named controls go
    through, so a control exercises the reading rather than a copy of it.
    """
    if facts.get("unreadable"):
        return "unreadable"
    if facts["has_result_record"]:
        return "completed"
    if terminal and alive is True:
        return "running"
    return "dead"


def recorded_runs(db_path: Path) -> dict[str, dict]:
    """Per-run role/effort/backend rows from the ledger's embedded store.

    Read through a read-only connection: the store is opened read-write by its
    own accessor, which migrates on open, and a census has no business writing
    to a store other sessions are promoting into.
    """
    import sqlite3

    rows: dict[str, dict] = {}
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        for run_id, payload in connection.execute(
            'SELECT "run_id", "payload" FROM "runs"'
        ):
            try:
                record = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            agent = record.get("agent")
            agent = agent if isinstance(agent, dict) else {}
            rows[str(run_id)] = {
                "backend": agent.get("backend"),
                "effort": agent.get("effort"),
                "role_record": record.get("role"),
            }
    finally:
        connection.close()
    return rows


def prompt_role(run_dir: Path) -> str | None:
    """The role the run's own dispatch prompt declares, if it declares one.

    An independent attribution source from the ledger: the prompt is the run's
    own artifact and names the role the worker was dispatched under.
    """
    try:
        text = (run_dir / "prompt.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^ROLE\s+(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def exit_signals(run_dir: Path) -> dict[str, dict]:
    """Signal each attempt's exit record names, where the supervisor wrote one."""
    out: dict[str, dict] = {}
    candidates = [("stream", run_dir / "attempt-1-exit.json")]
    candidates += [
        (f"resume-{rank}", run_dir / f"attempt-{rank + 1}-exit.json")
        for rank in range(1, 12)
    ]
    for label, path in candidates:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out[label] = {
            "signal": record.get("signal"),
            "signal_name": record.get("signal_name"),
            "ended_during": record.get("ended_during"),
            "exit_code": record.get("exit_code"),
            "exited_at": record.get("exited_at"),
        }
    return out


def day_of(stamp: str | None) -> str | None:
    return stamp[:10] if stamp and len(stamp) >= 10 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--runs-root", default=str(RUNS_ROOT))
    parser.add_argument("--live-dir", default=str(LIVE_DIR))
    parser.add_argument("--store", default=None)
    parser.add_argument("--control-run", default=CONTROL_RUN_ID)
    args = parser.parse_args(argv)

    write_context: dict = {}
    try:
        from reckon.run_store import store_path

        write_context["store"] = str(store_path())
    except Exception as exc:  # noqa: BLE001 - reported, never silent
        write_context["store_import_error"] = repr(exc)
    store = Path(args.store) if args.store else Path(write_context.get("store", ""))

    records = recorded_runs(store) if store.is_file() else {}

    from reckon.crew.death_census import pointer_process_alive

    runs_root = Path(args.runs_root)
    live_dir = Path(args.live_dir)

    attempts: list[dict] = []
    roles_disagree: list[dict] = []
    scanned_dirs = 0
    local_runs = 0
    local_dirs = 0
    role_prompt_declared = 0
    role_prompt_silent = 0
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        scanned_dirs += 1
        run_id = entry.name
        meta = records.get(run_id)
        if meta is None or meta.get("backend") != LOCAL_BACKEND:
            continue
        local_runs += 1
        declared_role = prompt_role(entry)
        streams = attempt_streams(entry)
        if not streams:
            continue
        local_dirs += 1
        if declared_role is None:
            role = meta.get("role_record")
            if role is not None:
                role_prompt_silent += 1
        else:
            role_prompt_declared += 1
            role = declared_role
            if meta.get("role_record") not in (None, declared_role):
                roles_disagree.append(
                    {
                        "run_id": run_id,
                        "prompt_role": declared_role,
                        "recorded_role": meta.get("role_record"),
                    }
                )
        signals = exit_signals(entry)
        alive = pointer_process_alive(run_id, live_dir)
        for index, (label, path) in enumerate(streams):
            facts = stream_facts(path)
            times = stream_times(path)
            terminal = index == len(streams) - 1
            classification = classify_attempt(
                facts, alive=alive if terminal else None, terminal=terminal
            )
            exit_fact = signals.get(label, {})
            attempts.append(
                {
                    "run_id": run_id,
                    "attempt": label,
                    "terminal_attempt": terminal,
                    "role": role,
                    "effort": meta.get("effort"),
                    "backend": meta.get("backend"),
                    "classification": classification,
                    "has_result_record": bool(facts["has_result_record"]),
                    "last_record_type": facts["last_record_type"],
                    "last_record_subtype": facts["last_record_subtype"],
                    "record_count": facts["record_count"],
                    "parse_failures": facts["parse_failures"],
                    "started_at": times["first"],
                    "last_at": times["last"],
                    "time_source": times["source"],
                    "signal_name": exit_fact.get("signal_name"),
                    "ended_during": exit_fact.get("ended_during"),
                }
            )

    # ── aggregates ──────────────────────────────────────────────────────────
    def rate(dead: int, size: int) -> float | None:
        return round(dead / size, 4) if size else None

    cells: dict[tuple, dict] = {}
    for row in attempts:
        key = (str(row["role"]), str(row["effort"]))
        cell = cells.setdefault(
            key,
            {
                "role": key[0],
                "effort": key[1],
                "attempts": 0,
                "completed": 0,
                "dead": 0,
                "running": 0,
                "unreadable": 0,
            },
        )
        cell["attempts"] += 1
        classification = row["classification"]
        if classification in ("completed", "dead", "running"):
            cell[classification] += 1
        else:
            cell["unreadable"] += 1
    for cell in cells.values():
        cell["death_rate"] = rate(cell["dead"], cell["attempts"])

    per_day: dict[str, Counter] = defaultdict(Counter)
    deaths: list[dict] = []
    for row in attempts:
        if row["classification"] != "dead":
            continue
        day = day_of(row["last_at"])
        per_day[day][str(row["role"])] += 1
        if str(row["role"]) == "review":
            deaths.append(
                {
                    "run_id": row["run_id"],
                    "attempt": row["attempt"],
                    "role": row["role"],
                    "effort": row["effort"],
                    "last_at": row["last_at"],
                    "last_record_type": row["last_record_type"],
                    "record_count": row["record_count"],
                    "signal_name": row["signal_name"],
                    "ended_during": row["ended_during"],
                }
            )

    review_cell = next(
        (
            c
            for c in cells.values()
            if c["role"] == "review" and c["effort"] == REVIEW_EFFORT
        ),
        None,
    )
    total_dead = sum(1 for row in attempts if row["classification"] == "dead")
    review_attempts = sum(1 for row in attempts if str(row["role"]) == "review")
    review_dead = sum(
        1
        for row in attempts
        if str(row["role"]) == "review" and row["classification"] == "dead"
    )

    stamps = [row["started_at"] for row in attempts if row["started_at"]]
    window = {
        "start": min(stamps) if stamps else None,
        "end": max(
            (row["last_at"] for row in attempts if row["last_at"]), default=None
        ),
    }
    scan_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── controls ────────────────────────────────────────────────────────────
    # A census of deaths is only as good as its reader's ability to see the
    # other outcome: a parser anchored on the wrong record shape reports every
    # run dead; the shape of the harness's own result record was misread here
    # once. Both directions are therefore named.
    control_dir = runs_root / args.control_run
    control_attempts: list[dict] = []
    if control_dir.is_dir():
        control_signals = exit_signals(control_dir)
        for label, path in attempt_streams(control_dir):
            facts = stream_facts(path)
            control_attempts.append(
                {
                    "attempt": label,
                    "stream": path.name,
                    "record_count": facts["record_count"],
                    "has_result_record": bool(facts["has_result_record"]),
                    "last_record_type": facts["last_record_type"],
                    "last_record_subtype": facts["last_record_subtype"],
                    "classification": classify_attempt(facts),
                }
            )
    death_control = next(
        (a for a in control_attempts if a["classification"] == "dead"), None
    )
    if death_control is not None:
        label = death_control["attempt"]
        death_control["run_id"] = args.control_run
        death_control["role"] = prompt_role(control_dir)
        death_control["effort"] = records.get(args.control_run, {}).get("effort")
        death_control["last_at"] = stream_times(control_dir / death_control["stream"])[
            "last"
        ]
        corroboration = control_signals.get(label, {})
        death_control["signal_name"] = corroboration.get("signal_name")
        death_control["ended_during"] = corroboration.get("ended_during")
        death_control["signal_exit_record"] = bool(corroboration)

    completion_control = next(
        (a for a in control_attempts if a["classification"] == "completed"), None
    )
    if completion_control is not None:
        completion_control["run_id"] = args.control_run
        completion_control["note"] = (
            "same run, later attempt: the parser reads a result record where one "
            "was written, so a death reading is not the reader failing on this run"
        )

    # The rate the fence would face now, beside the whole-history rate.
    latest_day = max((day for day in per_day if day), default=None)
    trailing: dict[str, dict] = {}
    if latest_day is not None:
        end_day = datetime.strptime(latest_day, "%Y-%m-%d").replace(tzinfo=UTC).date()
        for span in (1, 7):
            cutoff = end_day.fromordinal(end_day.toordinal() - span + 1).isoformat()
            window_attempts = [
                row
                for row in attempts
                if str(row["role"]) == "review"
                and (day_of(row["last_at"]) or "") >= cutoff
            ]
            window_dead = [r for r in window_attempts if r["classification"] == "dead"]
            trailing[f"trailing_{span}d"] = {
                "from": cutoff,
                "to": latest_day,
                "population": "review role at every declared effort",
                "review_attempts": len(window_attempts),
                "review_deaths": len(window_dead),
                "rate": rate(len(window_dead), len(window_attempts)),
            }

    payload = {
        "measure": "local-lane mid-turn deaths classified from the runs' own streams",
        "lane": {
            "backend": LOCAL_BACKEND,
            "model": "deepseek-v4.1-flash",
            "effort_declared": "xhigh",
            "lane_source": "host flight config, local_backend key",
        },
        "method": {
            "attempt_view": (
                "every stream.jsonl / resume-N.jsonl is one observation; a death is a stream "
                "with no top-level result record whose process is gone"
            ),
            "terminal_view": (
                "only the run's last attempt is read, matching reckon.crew.death_census"
            ),
            "role_source": "the run's own prompt.txt ROLE line, ledger record as fallback",
            "effort_source": "the run's recorded ledger row (the lane pins effort)",
            "liveness": "reckon.crew.death_census.pointer_process_alive on the live pointer",
            "effort_is_not_a_calibration_axis_here": (
                "the lane's endpoint ignores thinking budgets, so the review reflex's effort "
                "is the lane's declared one"
            ),
            "streams_root": str(runs_root),
            "measurement_script": MEASUREMENT_SCRIPT,
            "invocation": (
                "timeout 1200 <venv>/bin/python "
                "docs/research/scripts/scan_local_lane_deaths.py "
                "--out docs/research/data/local-lane-deaths.json"
            ),
            "scan_time_utc": scan_time,
        },
        "before": {
            "window": window,
            "population": {
                "run_directories_scanned": scanned_dirs,
                "runs_on_local_lane": local_runs,
                "runs_on_local_lane_with_streams": local_dirs,
                "attempts_on_local_lane": len(attempts),
                "review_attempts_all_efforts": review_attempts,
            },
            "review_effort": REVIEW_EFFORT,
            "deaths": {
                "view": "attempt",
                "headline": {
                    "population": "review role at the lane's declared review effort",
                    "role": "review",
                    "effort": REVIEW_EFFORT,
                    "count": (review_cell or {}).get("dead", 0),
                    "attempts": (review_cell or {}).get("attempts", 0),
                    "rate": (review_cell or {}).get("death_rate"),
                },
                "review_role_all_efforts": {
                    "population": "review role at every declared effort",
                    "count": review_dead,
                    "attempts": review_attempts,
                    "rate": rate(review_dead, review_attempts),
                },
                "all_roles": {
                    "population": "every role at every effort",
                    "count": total_dead,
                },
            },
            "trailing": trailing,
            "positive_control": death_control,
            "completion_control": completion_control,
            "cells": sorted(
                cells.values(), key=lambda c: (-c["attempts"], c["role"], c["effort"])
            ),
            "deaths_per_day_by_role": {
                str(day): dict(counter) for day, counter in sorted(per_day.items())
            },
            "role_attribution": {
                "runs_on_local_lane": local_runs,
                "runs_with_streams": local_dirs,
                "prompt_declares_role": role_prompt_declared,
                "prompt_silent_fell_back_to_ledger": role_prompt_silent,
                "disagreements_recorded": len(roles_disagree),
            },
            "role_attribution_disagreements": roles_disagree,
            "review_deaths_all_efforts": deaths,
        },
        "after": None,
        "after_state": "not_produced",
        "after_unavailable_reason": (
            "the repair that would create a post-repair window has not landed: "
            "the review-reliability work that owns it is declared implementable "
            "with its driving followup still open, so no attempt in the corpus "
            "ran against a repaired lane."
        ),
    }
    if review_cell is not None:
        payload["before"]["review_cell"] = review_cell

    Path(args.out).write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    size = Path(args.out).stat().st_size
    print(
        f"wrote {args.out} ({size} bytes) attempts={len(attempts)} "
        f"review_dead={review_dead}/{review_attempts} all_dead={total_dead}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
