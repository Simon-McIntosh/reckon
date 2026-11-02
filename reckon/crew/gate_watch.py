"""The rows a coordinator's own gate runs draw on its pane.

A coordinator's focused gate can run for an hour, and while it does the
session is doing the only work it has: waiting on its own check. Nothing about
the run is a worker, so the pane's worker count reads ``0w`` and the pane has
nothing to draw — which reads to the coordinator, and to the lead beside them,
as a broken monitor rather than as a session blocked on itself.

``reckon crew verify-gate`` and ``reckon crew suite run`` are the two verbs a
coordinator runs a gate with, and neither wrote anything a producer reads until
it recorded its report at the end. This module closes that gap on both sides of
the record.

**The record.** One durable, workstation-wide document holds every gate run:
the calling coordinator's session, the project, the run id when one is named,
the checkout, the command's first words, the start instant and the pid that
runs it, and at the end the exit status. A start writes the record; the verb
completes it when the gate returns, whatever the exit status.

**An end needs no writer.** A gate's process disappearing without a completed
record — a killed CLI, a dropped ssh, a machine that went away — reads as ended
with no exit status rather than as running for ever. That is a read, not a
write: every project's producer folds the document, so a producer that stamped
an end would race its peers over one file. The record's pid and the kernel's
start tick for it are what the liveness read compares, so a pid the kernel has
since handed to another process is not mistaken for the gate.

**The rows.** A sweep folds the document the way :mod:`reckon.crew.lift_watch`
folds budget lifts: one row when a gate starts, one when it ends, and — for a
producer that has just (re)armed — a baseline row for every gate already
running rather than announcing it as new. Each row names the gate's own session,
so it reaches that session's pane and no other.

A gate becomes due when its process ends, which moves no file, so this sweep
rides the producer's clock-driven block beside the limit-reset sweep rather
than the change gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import _store as store
from reckon.crew import obligation_snapshot, process_liveness, recovery_watch

GATES_LEAF = "crew-gates.json"

# The rows' destination states. The renderer spells the same two words itself
# and carries them, because this module imports the recovery watch, which
# imports the renderer — reading them back from here would close an import
# cycle. A test binds the two spellings together.
GATE_RUNNING = "gate-running"
GATE_ENDED = "gate-ended"

#: How far back the document is kept. A gate record exists to be folded onto a
#: pane while the gate runs and once when it ends, so a record an hour past its
#: end is history the stream already carries. Pruning keeps the document the
#: size of one coordinator's working day rather than of every gate ever run.
GATE_HORIZON = timedelta(hours=12)
#: A ceiling on the document besides the horizon, so a fleet running many short
#: gates in one day cannot grow the file without bound.
MAX_RECORDS = 200

VERIFY_GATE = "verify-gate"
SUITE_RUN = "suite run"

#: How many words of the command a record keeps. The pane has no room for a
#: whole command line, and the first words are what identifies it.
COMMAND_WORDS = 6

__all__ = [
    "GATES_LEAF",
    "GATE_ENDED",
    "GATE_HORIZON",
    "GATE_RUNNING",
    "SUITE_RUN",
    "VERIFY_GATE",
    "end",
    "gate_records_path",
    "read_document",
    "running",
    "start",
    "sweep",
]


def gate_records_path(home: Path | None = None) -> Path:
    """Return the gate document's path, directly under the shared config home."""
    return (home if home is not None else store._config_home()) / GATES_LEAF


def read_document(path: str | Path | None = None) -> dict[str, Any]:
    """Read the gate document, returning an empty one when none is written.

    A missing file, an unreadable one and one carrying no ``gates`` list are all
    an empty record rather than an error: a workstation that has never run a
    gate and one whose document cannot be read both fold no gate row, and no row
    is what the pane showed before this module existed.
    """
    target = Path(path) if path is not None else gate_records_path()
    if not target.exists():
        return {"version": 0, "gates": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 0, "gates": []}
    if not isinstance(payload, Mapping):
        return {"version": 0, "gates": []}
    gates = payload.get("gates")
    version = payload.get("version")
    return {
        "version": int(version) if isinstance(version, int) else 0,
        "gates": [dict(gate) for gate in gates] if isinstance(gates, list) else [],
    }


def start(
    project: str,
    *,
    verb: str,
    checkout: str | Path,
    command: str,
    session: str = "",
    run_id: str = "",
    pid: int | None = None,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Write one gate's start record and return it.

    The record names the session whose pane the row reaches, the project and run
    it belongs to, the checkout the gate measures, the command's first words and
    the instant and process the gate began at. It is written before the gate
    runs, so a gate that outlives the caller's patience is still on record.
    """
    moment = _aware(now)
    target = Path(path) if path is not None else gate_records_path()
    record: dict[str, Any] = {
        "id": f"{moment.strftime('%Y%m%dT%H%M%S%f')}-{verb.replace(' ', '-')}",
        "project": str(project),
        "verb": str(verb),
        "session": str(session),
        "run_id": str(run_id or ""),
        "checkout": str(checkout),
        "command": _first_words(command),
        "started_at": moment.isoformat(),
        "pid": int(pid) if pid is not None else None,
        "pid_start_time": obligation_snapshot.process_start_time(pid),
        "ended_at": None,
        "exit_status": None,
    }
    with store._serialized_path_lock(target, "gates"):
        document = read_document(target)
        gates = _pruned(document.get("gates") or [], moment)
        gates.append(record)
        _write(document, gates, target)
    return record


def end(
    gate_id: str,
    *,
    exit_status: int | None,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Complete one gate's record with the status its command ended at.

    A record already marked ended is left as it is: the end is the record's own
    account of what happened, and a second writer arriving later — a resumed
    caller, or a sweep run by hand — must not rewrite it.
    """
    moment = _aware(now)
    target = Path(path) if path is not None else gate_records_path()
    with store._serialized_path_lock(target, "gates"):
        document = read_document(target)
        gates = [dict(gate) for gate in document.get("gates") or []]
        found = next(
            (gate for gate in gates if str(gate.get("id") or "") == str(gate_id)),
            None,
        )
        if found is None:
            return {}
        if not found.get("ended_at"):
            found["ended_at"] = moment.isoformat()
            found["exit_status"] = exit_status
            _write(document, gates, target)
        return dict(found)


def running(
    *,
    now: datetime | None = None,
    alive: Callable[[Any], bool | None] | None = None,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Every gate still running, each read as it stands.

    A gate is running while its record carries no end and the process it names
    is alive. A record whose process is gone reads as ended with no exit status
    — the gate did not report, so the pane says only that it stopped. A record
    naming no process is not proof of death, so it stays running until an end is
    written, the same stance the lift fold takes toward an unreadable document.
    """
    gates = list(read_document(path).get("gates") or [])
    return [gate for gate in gates if _is_running(gate, alive=alive)]


def _is_running(
    gate: Mapping[str, Any], *, alive: Callable[[Any], bool | None] | None = None
) -> bool:
    if gate.get("ended_at"):
        return False
    return process_liveness.record_process_alive(dict(gate), alive=alive) is not False


def sweep(
    project: str,
    *,
    seen: Mapping[str, Any] | None = None,
    alive: Callable[[Any], bool | None] | None = None,
    stream_path: str | Path | None = None,
    append: Callable[[Path, Sequence[Mapping[str, Any]]], None] | None = None,
    path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Fold the gate document into watch-stream rows and return the next memory.

    ``seen`` is the producer's memory of the gates it has already announced, in
    the shape this function returns: ``None`` on the first pass after a
    (re)arm, so every gate already running is written as a baseline rather than
    announced as new, and a mapping of gate id to its record afterwards. A gate
    in ``seen`` that is no longer running writes one ended row carrying its exit
    status when the record has one; a gate running and not yet in ``seen``
    writes one start row.

    Nothing is appended when no gate moved, so an unchanged document costs no
    stream write. Each row names the gate's own session, so the pane it reaches
    is that session's and no other.
    """
    gates = [dict(gate) for gate in read_document(path).get("gates") or []]
    current = [gate for gate in gates if _is_running(gate, alive=alive)]
    by_id = {str(gate.get("id") or ""): gate for gate in gates}

    rows: list[dict[str, Any]] = []
    if seen is None:
        rows.extend(_gate_row(project, gate, kind="baseline") for gate in current)
    else:
        for gate in current:
            gate_id = str(gate.get("id") or "")
            if gate_id and gate_id not in seen:
                rows.append(_gate_row(project, gate, kind="transition"))
        for gate_id in seen:
            if any(str(gate.get("id") or "") == gate_id for gate in current):
                continue
            gate = by_id.get(gate_id)
            if gate is not None:
                rows.append(_gate_row(project, gate, kind="transition", ended=True))

    if rows:
        _append_rows(project, stream_path, rows, append)
    return {str(gate.get("id") or ""): gate for gate in current}


def _write(
    document: Mapping[str, Any], gates: Sequence[Mapping[str, Any]], target: Path
) -> Path:
    next_version = int(document.get("version", 0)) + 1
    return store.write_json_atomically(
        target, {"version": next_version, "gates": list(gates)}
    )


def _pruned(gates: Sequence[Any], now: datetime) -> list[dict[str, Any]]:
    """The records worth keeping: recent ones, up to the ceiling."""
    kept: list[dict[str, Any]] = []
    for gate in gates:
        if not isinstance(gate, Mapping):
            continue
        stamp = _parse(gate.get("started_at"))
        if stamp is None:
            continue
        if now - stamp > GATE_HORIZON:
            continue
        kept.append(dict(gate))
    return kept[-MAX_RECORDS:]


def _append_rows(
    project: str,
    stream_path: str | Path | None,
    rows: Sequence[Mapping[str, Any]],
    append: Callable[[Path, Sequence[Mapping[str, Any]]], None] | None,
) -> None:
    from reckon.crew import runs

    target = (
        Path(stream_path)
        if stream_path is not None
        else runs.watch_stream_path(project)
    )
    writer = append if append is not None else runs._append_watch_lines
    writer(target, rows)


def _gate_row(
    project: str,
    gate: Mapping[str, Any],
    *,
    kind: str,
    ended: bool = False,
) -> dict[str, Any]:
    """One gate row, built as a transition the established formatter renders.

    The row carries the fields an ordinary run transition carries, so a follower
    on current main renders it without raising; what the gate is and where it
    runs rides ``detail``, the free-text field the formatter already knows. The
    gate's session is the row's, so the pane it reaches is that session's own.
    """
    if ended:
        previous, current = GATE_RUNNING, GATE_ENDED
    elif kind == "baseline":
        previous, current = None, GATE_RUNNING
    else:
        previous, current = None, GATE_RUNNING
    snapshot = {
        "run_id": None,
        "node": str(gate.get("verb") or ""),
        "session": str(gate.get("session") or ""),
        "role": "",
        "backend": "",
        "model": "",
        "effort": "",
        "alias": "",
        "classification": None,
        "process_alive": None,
        "liveness_proven": None,
        "detail": _gate_detail(gate, ended=ended),
        "recovery_classification": current,
    }
    row = recovery_watch._watch_transition(
        project,
        kind=kind,
        snapshot=snapshot,
        previous=previous,
        current=current,
        counts={"working": 0, "blocked": 0, "unpromoted": 0},
        spend_runs=[],
    )
    row["gate_id"] = str(gate.get("id") or "")
    return row


def _gate_detail(gate: Mapping[str, Any], *, ended: bool) -> str:
    """The clause a gate row carries, reading plainly in the pane.

    For a gate still running it names the verb, the checkout and the command's
    first words, the instant it began and the run it measures. For one that has
    ended it leads with the exit status, or says the process went away without
    reporting one, so the reader keeps the verdict when the clause is cut to the
    pane.
    """
    verb = str(gate.get("verb") or "gate")
    checkout = str(gate.get("checkout") or "")
    command = str(gate.get("command") or "")
    run_id = str(gate.get("run_id") or "")
    named = f"{verb} in {checkout}, `{command}`"
    if run_id:
        named = f"{named}, run {run_id}"
    if ended:
        exit_status = gate.get("exit_status")
        if exit_status is None:
            verdict = "ended without reporting an exit status"
        else:
            verdict = f"ended with exit status {exit_status}"
        return f"coordinator gate {verdict}: {named}"
    stamp = _parse(gate.get("started_at"))
    since = (
        f" since {stamp.astimezone(UTC).strftime('%H:%M')} UTC"
        if stamp is not None
        else ""
    )
    return f"coordinator gate running{since}: {named}"


def _first_words(command: str) -> str:
    words = str(command or "").split()
    return " ".join(words[:COMMAND_WORDS])


def _parse(value: Any) -> datetime | None:
    from reckon._timestamps import parse_utc

    return parse_utc(value)


def _aware(now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
