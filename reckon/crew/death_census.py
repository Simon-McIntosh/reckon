"""Separate a run's death from its completion by the terminal shape of its stream.

A worker ended by a signal writes nothing on the way out, so its stream simply
stops mid-thought. The one line that separates that case from a run that
finished is the presence of a ``result`` record: a client that reached the end
of a turn emits one, whether the turn succeeded or failed, while a process gone
mid-stream emitted none. The record's own success flag is deliberately not
consulted — a failed turn still wrote a result record, so it still says the
turn ended, and treating it as a death would relabel an ordinary failure as
this subject.

A run's stream is its **terminal attempt**: the highest-numbered resume stream
when the run was resumed, otherwise the initial one. An earlier attempt's stop
is not where the run ended, so reading it would count a resumed-and-finished
run as dead.

Classification of one run is therefore the pair (result record present,
process gone):

* result present           -> ``completed``
* result absent, alive      -> ``running`` (in flight; never a death)
* result absent, gone       -> ``dead``

``process_alive`` is derived from the run's live pointer when one exists — its
pid and recorded start tick, through the same reader every other liveness
decision uses, because a pointer's stored ``process_alive`` field is usually
absent and trusting it would read a live run as gone. A run with no pointer
reads gone, because a pointer is removed when a run is promoted.

The recorded role, effort and sandbox are read from the run ledger's embedded
store, one row per run id; a run directory with no recorded row cannot be
attributed to a lane and is reported as unattributed rather than guessed at.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any

from reckon.crew.runs import record_process_alive

CLIVE_BACKEND = "clive"
_RESULT_TYPE = "result"
_ATTEMPT_PREFIX = "resume-"
_ATTEMPT_SUFFIX = ".jsonl"
_INITIAL_STREAM = "stream.jsonl"

CLASS_COMPLETED = "completed"
CLASS_DEAD = "dead"
CLASS_RUNNING = "running"
CLASS_UNREADABLE = "unreadable"


def terminal_stream_path(run_dir: str | PathLike[str]) -> Path | None:
    """Return the stream holding the run's terminal attempt, or None.

    Resume streams are numbered; the highest is the run's last attempt. The
    initial stream wins only when no resume stream exists.
    """
    directory = Path(run_dir)
    if not directory.is_dir():
        return None
    best: tuple[int, Path] | None = None
    initial: Path | None = None
    for entry in sorted(directory.iterdir()):
        name = entry.name
        if name == _INITIAL_STREAM:
            initial = entry
        elif name.startswith(_ATTEMPT_PREFIX) and name.endswith(_ATTEMPT_SUFFIX):
            stem = name[len(_ATTEMPT_PREFIX) : -len(_ATTEMPT_SUFFIX)]
            if stem.isdigit():
                rank = int(stem)
                if best is None or rank > best[0]:
                    best = (rank, entry)
    if best is not None:
        return best[1]
    return initial


def stream_facts(stream_path: str | PathLike[str]) -> dict[str, Any]:
    """Read one stream's terminal shape without trusting any record's content.

    Returns record count, whether any ``result`` record appears, the type and
    subtype of the last record, and how many lines failed to parse. A line that
    is not a JSON object is counted as a parse failure, never as a record, so a
    truncated tail cannot masquerade as a terminal event.
    """
    path = Path(stream_path)
    facts: dict[str, Any] = {
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
            if not isinstance(event, Mapping):
                facts["parse_failures"] += 1
                continue
            facts["record_count"] += 1
            event_type = event.get("type")
            if event_type == _RESULT_TYPE:
                facts["has_result_record"] = True
            subtype = event.get("subtype")
            facts["last_record_type"] = event_type
            facts["last_record_subtype"] = subtype if isinstance(subtype, str) else None
    return facts


def classify(facts: Mapping[str, Any], *, process_alive: bool | None) -> str:
    """Name one run's outcome from its stream shape and whether it is alive.

    ``process_alive`` is True for an in-flight run, False for a gone one. None
    is treated as gone: a run with no live pointer has been promoted or
    reclaimed, and its process is no longer running.
    """
    if facts.get("unreadable"):
        return CLASS_UNREADABLE
    if facts.get("has_result_record"):
        return CLASS_COMPLETED
    if process_alive is True:
        return CLASS_RUNNING
    return CLASS_DEAD


def pointer_process_alive(run_id: str, live_dir: str | PathLike[str]) -> bool | None:
    """Whether the run's live pointer names a process that is still running.

    Liveness is derived from the pointer's own pid and recorded start tick
    rather than read from a stored ``process_alive`` field: most live pointers
    carry no such field, so a reader that trusted it would call a live run gone
    and count it a death. None means no pointer exists, which is a run whose
    process is no longer running rather than a live one.
    """
    path = Path(live_dir) / f"{run_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    return record_process_alive(record)


def recorded_runs(db_path: str | PathLike[str]) -> dict[str, dict[str, Any]]:
    """Read every recorded run's lane attributes from the ledger's store.

    Returns run id -> {backend, effort, sandbox, role}. Missing or stale index
    membership is rebuilt from the committed records before answering.
    """
    from reckon.run_store import RunStore

    records: dict[str, dict[str, Any]] = {}
    with RunStore(Path(db_path)) as store:
        rows = store.records()
    for run_id, record in rows.items():
        agent = record.get("agent")
        agent = agent if isinstance(agent, Mapping) else {}
        records[run_id] = {
            "backend": agent.get("backend"),
            "effort": agent.get("effort"),
            "sandbox": agent.get("sandbox"),
            "role": record.get("role"),
        }
    return records


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def census_runs(
    runs_dir: str | PathLike[str],
    records: Mapping[str, Mapping[str, Any]],
    *,
    backend: str = CLIVE_BACKEND,
    live_dir: str | PathLike[str] | None = None,
) -> dict[str, Any]:
    """Census every run directory attributed to one backend's lane.

    Each run directory is classified from its own stream, then joined to its
    recorded role, effort and sandbox. Runs are grouped into role-by-effort
    cells; each cell carries its size and its death rate, where the denominator
    is the cell's own size and a running (in-flight) run is not a death.

    The ``positive_control`` block names one completed run the parser read a
    result record from, so a reader can see the parser saw something rather
    than inferring its aim from counts alone.
    """
    root = Path(runs_dir)
    live = Path(live_dir) if live_dir is not None else None
    runs: list[dict[str, Any]] = []
    unattributed: list[str] = []
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if not entry.is_dir():
            continue
        run_id = entry.name
        meta = records.get(run_id)
        if meta is None or meta.get("backend") != backend:
            continue
        if (
            not (entry / _INITIAL_STREAM).is_file()
            and terminal_stream_path(entry) is None
        ):
            continue
        stream = terminal_stream_path(entry)
        if stream is None:
            continue
        facts = stream_facts(stream)
        alive = pointer_process_alive(run_id, live) if live is not None else None
        outcome = classify(facts, process_alive=alive)
        run = {
            "run_id": run_id,
            "role": meta.get("role"),
            "effort": meta.get("effort"),
            "sandbox": meta.get("sandbox"),
            "stream": stream.name,
            "has_result_record": bool(facts.get("has_result_record")),
            "last_record_type": facts.get("last_record_type"),
            "last_record_subtype": facts.get("last_record_subtype"),
            "record_count": facts.get("record_count"),
            "classification": outcome,
        }
        runs.append(run)
        if run["role"] is None and run["effort"] is None:
            unattributed.append(run_id)

    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        key = (str(run["role"]), str(run["effort"]))
        cell = cells.setdefault(
            key,
            {
                "role": key[0],
                "effort": key[1],
                "size": 0,
                "completed": 0,
                "dead": 0,
                "running": 0,
                "unreadable": 0,
            },
        )
        cell["size"] += 1
        classification = run["classification"]
        if classification in (CLASS_COMPLETED, CLASS_DEAD, CLASS_RUNNING):
            cell[classification] += 1
        else:
            cell["unreadable"] += 1
    for cell in cells.values():
        cell["death_rate"] = _pct(cell["dead"], cell["size"])
        cell["completion_rate"] = _pct(cell["completed"], cell["size"])

    positive = next(
        (run for run in runs if run["classification"] == CLASS_COMPLETED), None
    )
    totals: dict[str, int] = defaultdict(int)
    for run in runs:
        totals[run["classification"]] += 1
    return {
        "backend": backend,
        "run_count": len(runs),
        "totals": dict(totals),
        "cells": sorted(
            cells.values(), key=lambda c: (-c["size"], c["role"], c["effort"])
        ),
        "runs": runs,
        "unattributed": unattributed,
        "positive_control": positive,
    }


def _manifest_status(run_dir: Path) -> str | None:
    """The status line a run's manifest carries, or None if unreadable."""
    try:
        text = (run_dir / "manifest.md").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip() or None
    return None


def completed_control(
    runs_dir: str | PathLike[str],
    records: Mapping[str, Mapping[str, Any]],
    *,
    backend: str = CLIVE_BACKEND,
) -> dict[str, Any] | None:
    """Read one run whose own manifest says it completed, as a positive control.

    A census that reports no dead runs and a census whose parser never saw a
    live record return the same counts. This names a run the record itself
    calls complete and shows the parser read its result record, so a reader can
    see the parser was aimed at something known present.
    """
    root = Path(runs_dir)
    for run_id in sorted(records):
        meta = records[run_id]
        if meta.get("backend") != backend:
            continue
        run_dir = root / run_id
        if _manifest_status(run_dir) != "complete":
            continue
        stream = terminal_stream_path(run_dir)
        if stream is None:
            continue
        facts = stream_facts(stream)
        return {
            "run_id": run_id,
            "manifest_status": "complete",
            "stream": stream.name,
            "has_result_record": bool(facts.get("has_result_record")),
            "last_record_type": facts.get("last_record_type"),
            "last_record_subtype": facts.get("last_record_subtype"),
            "classification": classify(facts, process_alive=False),
            "reports_dead": classify(facts, process_alive=False) == CLASS_DEAD,
        }
    return None


def main(argv: list[str] | None = None) -> int:
    """Write the death-rate census over the locally served lane to JSON."""
    from datetime import UTC, datetime

    from reckon.crew import runs as runs_module
    from reckon.run_store import store_path

    out = (
        Path(argv[0])
        if argv
        else Path("docs/research/data/review-death-by-effort.json")
    )
    records = recorded_runs(store_path())
    root = runs_module.runs_dir()
    census = census_runs(root, records, live_dir=runs_module.live_dir())
    census["generated_by"] = "reckon.crew.death_census"
    census["generated_at_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    census["plan"] = "a-review-dies-where-nobody-is-looking"
    census["section"] = "§2"
    census["question"] = (
        "Separating the review death rate by role and by effort over every "
        "recorded run on the locally served lane."
    )
    census["method"] = {
        "stream": "the run's terminal attempt (highest resume-N.jsonl else stream.jsonl)",
        "dead": "no result record in the stream and no live pointer reporting the process alive",
        "stream_source": str(root),
        "recorded_source": str(store_path()),
    }
    census["positive_control_manifest_complete"] = completed_control(root, records)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(census, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {out}: {census['run_count']} runs, totals={census['totals']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
