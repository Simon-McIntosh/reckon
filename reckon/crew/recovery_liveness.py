from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.capabilities import _charged_input_from_usage
from reckon.crew import lane_document as _lane_document
from reckon.crew import metering, plan_review, quota_weight, runs
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_need
from reckon.crew.host_lease import LEASE_RENEW_SECONDS
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    NON_TERMINAL_MANIFEST_STATUSES,
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _manifest_freshness,
    _mutate_pointer,
    _process_start_time,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    producer_lease_seconds,
    read_pointer,
    update_watch_registration,
    watch_lease_renewed_at,
    watch_lock_path,
)
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label

process_alive = runs.process_alive
# The size each readable manifest last had, with the moment it was read, keyed by
# the run and attempt that owns the path. A file that shrank since that read is
# the signature of a truncating rewrite caught between the truncate and the
# write, so the previous reading stands — earlier than a normal rewrite, before
# its mtime has moved — and it expires with the same short window so it can
# never hold the unwritten reading back forever.
_MANIFEST_SIZES_READ: dict[str, tuple[int, float]] = {}
# A reader classifies every run it is shown, over days, so the size memory is
# capped and drops its least recently touched entry.
MANIFEST_SIZE_MEMORY_MAX = 256


# The run directory's own record of the worker's pid. A supervised launch
# writes the supervisor's pid on the pointer and the worker's pid here, so the
# two answer different questions: the pointer pid says whether the launcher
# still lives, this record says whether the work does.
WORKER_RECORD_NAME = "worker.json"

# The phases a run holds before its worker has been spawned and observed. A run
# that died in one of them recorded no worker and no exit, so its pointer pid
# going silent is not proof that any work stopped.
_PRE_SPAWN_PHASES = frozenset({"starting", "launching", "launcher", "dispatching"})

# A launch cut off between composing its record and spawning its worker leaves
# a pointer holding a pre-spawn phase and nothing else: no pid, no worker
# record, no stream and no launch log in its run directory. Nothing about it is
# in flight — there is no process to observe and no session to resume — and
# nothing about it ends either, so it holds whatever claim it took, a lane or a
# review another run is told is covered, for as long as the pointer lives. Past
# this bound the absence of any launch evidence is itself the reading and the
# pointer is a stranded launch. The bound matches the quiet window a dispatched
# run is given before it reads as stalled, so a launch that never spawned is
# called stranded on the same clock as one that spawned and went silent.
STRANDED_LAUNCH_BOUND_SECONDS = 900


def _observed_phase(
    phase: str,
    *,
    alive: bool | None,
    worker_alive: bool | None,
    worker_record_names_pid: bool,
    ended_exit: Mapping[str, Any] | None,
    manifest_status: str,
    commits_beyond_base: int,
    stream_shows_work: bool = False,
) -> str:
    """The phase a run's own evidence supports, not the last writer's label.

    A pointer's phase is written by the launcher: a supervisor sets it at spawn,
    and a run whose launch was interrupted can keep a pre-spawn label for its
    whole life. Where the stored phase is still one of those labels, the run's
    own evidence decides instead — a live worker record or retained commits
    show the launch got past starting. A live supervisor or stream alone does
    not: the supervisor may still be between admission and worker spawn, and a
    stream can be inherited from an earlier attempt. A terminal verdict on a
    gone process shows it finished. With no evidence at all the label stands:
    nothing has happened yet, and inventing an advance would be as wrong as
    inventing an end.

    A delivered manifest is itself evidence the launch got past starting: a
    worker cannot report a verdict before it has run. That holds whatever the
    process table says, so a delivered report never falls back to the
    launcher's pre-spawn label, which would render a finished run as
    dispatched. A process still reported alive outranks the report — the
    classifier reads that pairing as a deferred outcome, not a finished run —
    so the phase is working then.

    A worker record answers for every phase it was read in, not only while its
    process lives: the supervisor writes it once the worker is spawned, so its
    presence means the launch happened whatever the process table now says. An
    answer of "gone" is therefore evidence of the advance too, and only a
    record that names no pid leaves the label standing. Reading the answer as
    proof only while it was ``True`` let the phase fall back to the pre-spawn
    label the moment the worker exited, so a run that had already been reported
    working was reported dispatched again. Presence is the launch evidence and
    is read without the host gate that liveness carries: whether the pid can be
    probed *here* decides only whether the worker is alive now, while a record
    sitting in the run's directory proves the launch happened wherever it did,
    so a run whose launching host is another machine still advances past
    starting and is never rendered dispatched for it.

    An assistant record in the run's newest stream answers the same way, and it
    is the evidence left when nothing else has been written: the phase advances
    only when ``observe`` folds the stream, so a worker that has been thinking
    and editing for an hour keeps the label its launcher set until a reader
    happens to run one. The record is the worker's own first turn rather than a
    stream's mere existence, which an earlier attempt can leave behind, and it
    is consulted last so the stronger answers above decide first.
    """
    if phase not in _PRE_SPAWN_PHASES:
        return phase
    if manifest_status in TERMINAL_MANIFEST_STATUSES:
        return "working" if alive is True else "complete"
    if ended_exit is not None:
        return "complete"
    if worker_record_names_pid or commits_beyond_base:
        return "working"
    if stream_shows_work:
        return "working"
    return phase


def _carries_orientation_write(text: str, data: Mapping[str, Any]) -> bool:
    """Whether a manifest body is a worker's orientation write and nothing more.

    Every dispatch records where it is working before it has a status, so a body
    carrying the orientation keys and no status line is a run one minute into its
    life rather than a delivery that failed to declare a verdict. The raw body is
    read because the manifest reader requires a status key. A parsed body is
    accepted too, so a reader that once tolerated a missing status still lands
    here.

    A body with a ``status:`` line is not this case at all: whatever it says,
    the file has moved past its orientation write.
    """
    if data and data.get("orientation_worktree"):
        return not str(data.get("status") or "").strip()
    if "orientation_worktree" not in text:
        return False
    return not any(line.startswith("status:") for line in text.splitlines())


def _orientation_write_of_a_run_in_motion(
    record: Mapping[str, Any],
    *,
    alive: bool | None,
    worker_alive: bool | None,
    manifest_text: str,
    manifest_data: Mapping[str, Any],
) -> bool:
    """Whether a run holding only its orientation write is working right now.

    The file alone cannot answer this: a stub and the first minute of a turn are
    the same bytes, and the difference is whether the worker is still writing
    them. Liveness is that difference, and it must be a positive answer — a
    worker whose process is gone left its stub behind as the missing verdict it
    may well be, and the word for that stands. Beside liveness the reading wants
    the evidence the phase derivation uses for the same run, so the two surfaces
    cannot disagree: a worker record naming a pid, or an assistant turn in the
    run's newest stream, says the launch got past starting.
    """
    if alive is not True:
        return False
    if not _carries_orientation_write(manifest_text, manifest_data):
        return False
    if worker_alive is True:
        return True
    return _newest_stream_shows_work(record)


def _worker_record(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The run's own worker record, or None when none was written or readable."""
    try:
        data = json.loads((_run_directory(record) / WORKER_RECORD_NAME).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def _worker_record_liveness(record: Mapping[str, Any]) -> bool | None:
    """Whether the worker pid the run recorded for itself is still running.

    None answers "nothing to ask": no worker record, an unreadable one, or one
    naming no pid. Reported as its own fact rather than folded into the
    pointer's answer, because a supervisor that has exited before its worker
    takes the pointer pid with it while the work continues.

    The worker record carries no host of its own, so its pid is meaningful only
    on the machine that issued it: a number live here is no evidence about a run
    launched *elsewhere*, and reading it as one hands a foreign run a life this
    host cannot support. The read is therefore refused only for a run whose own
    launching host names a different machine. An unnamed host is not refused:
    the resumed-attempt deferral this fact exists for reads a run whose pointer
    cannot be resolved here, and a pointer written before the launching host was
    recorded names none, so refusing it would leave exactly the resumed run the
    deferral was built for with no liveness at all. The pid itself is decided by
    ``runs.record_process_alive``, which owns the start-tick comparison that
    keeps a recycled number from reading as the registered worker.
    """
    data = _worker_record(record)
    if data is None:
        return None
    if _record_is_known_foreign(record):
        return None
    return runs.record_process_alive(data, process_alive)


def _worker_launched_after_manifest(record: Mapping[str, Any], manifest: Path) -> bool:
    """Whether the run's current worker started after the manifest was written.

    A resumed attempt reuses its run directory, so the manifest beside the
    pointer may be the verdict a previous turn left. Its own launch time is the
    fact that separates the two: a worker that started after the manifest was
    last written cannot have written it, so a terminal status the file still
    carries belongs to the superseded attempt rather than to the worker running
    now. Unreadable evidence answers False — the reading never invents a launch
    it did not observe.
    """
    data = _worker_record(record)
    if data is None:
        return False
    raw = str(data.get("launched_at") or "").strip()
    if not raw:
        return False
    launched = parse_utc(raw)
    if launched is None:
        return False
    try:
        written = manifest.stat().st_mtime
    except OSError:
        return False
    return launched.timestamp() > written


def _int_or_none(value: Any) -> int | None:
    """A recorded number, or None when the record carries none to read."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _worker_record_pid(record: Mapping[str, Any]) -> int | None:
    """The pid the run's own worker record names, or None when it names none."""
    data = _worker_record(record)
    if data is None:
        return None
    return _int_or_none(data.get("pid"))


def _process_children(pid: Any) -> list[int] | None:
    """The pids the kernel lists as a process's own children, or None.

    The kernel's child list is read rather than every process's parent field,
    because one worker's children cost a read each while a host-wide scan costs
    a read per process on the machine. Every thread's list is read: a child may
    have been forked by a thread rather than by the process's first one. None
    answers "could not be read" — the process has gone, or this kernel does not
    publish the list — and is not the same answer as a process with no
    children, so a caller never reports an absence it did not observe.
    """
    number = _int_or_none(pid)
    if number is None:
        return None
    tasks = Path("/proc") / str(number) / "task"
    try:
        thread_ids = [entry.name for entry in tasks.iterdir()]
    except OSError:
        return None
    children: list[int] = []
    read_any = False
    for thread_id in thread_ids:
        try:
            listed = (tasks / thread_id / "children").read_text()
        except OSError:
            continue
        read_any = True
        children.extend(int(token) for token in listed.split() if token.isdigit())
    return children if read_any else None


def _live_descendant(pid: Any) -> bool:
    """Whether a running process has a running process under it.

    A worker waiting on a job — a build, a scheduler reservation, a probe — is
    a worker whose own stream says nothing while the child does the work, so a
    live child is the evidence on this host that something is still moving.
    One level of the child list answers the whole question: a process's
    children are reparented the moment it exits, so a running grandchild is
    always listed under a running child, and a child the kernel has finished is
    a table entry its parent has not yet reaped rather than a process running
    anything.
    """
    children = _process_children(pid)
    if children is None:
        return False
    return any(runs.process_alive(child) is True for child in children)


def _process_reading(
    alive: bool | None,
    *,
    liveness_proven: bool,
    exit_record: Mapping[str, Any] | None,
) -> str:
    """The three-way process state a row states in words.

    One reading, because three callers quote it and they must not disagree: a
    stalled row says which situation its stall is, and the resume offer asks
    whether the end was observed before it is made. A life is only the first of
    the three; death is claimed only where something observed it — a pid checked
    on the reading host, which the row's own ``liveness_proven`` records, or the
    supervisor's exit record, which survives a pointer nobody updated and a pid
    no other machine can look up. A stored answer carried because the launching
    host could not be shown to be this host is neither observation, and neither
    is no answer at all, so those read as unproven rather than as a death.
    """
    if alive is True:
        return "alive"
    if alive is False and (liveness_proven is True or exit_record is not None):
        return "process gone"
    return "liveness unknown"


def _record_newest_stream(record: Mapping[str, Any]) -> tuple[Path, float] | None:
    """A record's newest stream, through the shared reader."""
    return newest_stream(
        _run_directory(record), include=(record.get("log_path"),)
    )


def _run_stream_mtime(record: Mapping[str, Any]) -> float | None:
    """The newest write to any of the run's streams, or None when there is none.

    The stream is where an engine's own output lands, so its mtime is the one
    fact about a run that says it is producing something right now. Every
    stream a run has is considered, so a resumed or lane-changed run ages
    against the file it is writing now rather than the one it started with.
    Absent or unreadable is None rather than a zero: a run with no stream has
    taken no measurement, and a missing file must not read as infinitely stale.
    """
    found = _record_newest_stream(record)
    return found[1] if found is not None else None


# The run directory's own record of the current attempt's identity. A
# supervisor writes it before the attempt's worker can start, so it names the
# moment the attempt now running began even when the pointer predates the field.
ATTEMPT_RECORD_NAME = "attempt.json"


def _attempt_started_seconds(record: Mapping[str, Any]) -> float | None:
    """When the run's current attempt began, or None when nothing records it.

    The pointer carries ``attempt_started_at`` once a supervisor wrote it; a run
    whose pointer predates that field still names the same moment in the current
    attempt record the supervisor publishes beside it, so both are read. Absent
    from both means the run has no attempt clock, and its quiet time then falls
    back to the launch window a fresh dispatch gets.
    """
    raw = record.get("attempt_started_at")
    if not raw:
        try:
            marker = json.loads(
                (_run_directory(record) / ATTEMPT_RECORD_NAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            marker = None
        if isinstance(marker, Mapping):
            raw = marker.get("attempt_started_at")
    if not raw:
        return None
    started = parse_utc(str(raw))
    if started is None:
        return None
    return started.timestamp()


def _stranded_launch(record: Mapping[str, Any], *, now_seconds: float) -> bool:
    """Whether a launch that recorded nothing stopped happening past the bound.

    The phases a launch holds before it spawns are shared with a launch that is
    merely young, so the age alone cannot decide and neither can the phase:
    what separates the two is evidence. A spawned worker leaves a worker record,
    a stream, or a launch log in the run directory, so a pointer whose session
    wrote any of them is somewhere in flight whatever its phase says. With none
    of them written and the clock past the bound, there is nothing to wait for:
    the launch was cut off between composing the record and spawning the
    worker, and a pointer that holds a phase as though something were coming
    holds it forever.

    The clock is the current attempt's own start, which the launch composes on
    the pointer before spawning anything. A pointer naming no start has no
    launch clock to read and is left as one in flight.
    """
    if str(record.get("phase") or "") not in _PRE_SPAWN_PHASES:
        return False
    if record.get("pid"):
        return False
    if _worker_record(record) is not None:
        return False
    if _record_newest_stream(record) is not None:
        return False
    stderr_path = str(record.get("stderr_path") or "").strip()
    if stderr_path and Path(stderr_path).exists():
        return False
    started = _attempt_started_seconds(record)
    if started is None:
        return False
    return now_seconds - started > STRANDED_LAUNCH_BOUND_SECONDS


def _run_stream_quiet_seconds(
    record: Mapping[str, Any], *, now_seconds: float
) -> int:
    """Quiet time for a run, from its current attempt's own log and launch.

    Two clocks bound the reading, and the later of them decides. One is the
    newest stream the run has written: a resume or a superseded attempt writes
    a new file while the pointer keeps naming the old one, so the newest stream
    is the attempt's own output where one exists, and a fresh resume or lane
    change keeps the run working. The other is the attempt's launch, taken from
    the attempt record when a supervisor published one and otherwise from the
    same pointer-and-creation clock a fresh dispatch falls back to.

    A superseded attempt's stream must not age the run. A run resumed seconds
    ago reads ``stalled`` when its predecessor's stream is old and the resumed
    attempt has not written its own log yet, because the reading came from the
    attempt that already ended. Capping the stream's silence at the launch
    clock keeps such a run inside the window a fresh dispatch gets, and the
    clock grows with the run, so a resume that never writes its own log still
    stalls once that window has genuinely elapsed.
    """
    found = _record_newest_stream(record)
    if found is None:
        quiet = _stream_quiet_seconds(record, now_seconds=now_seconds)
    else:
        quiet = max(0, int(now_seconds - found[1]))
    attempt_started = _attempt_started_seconds(record)
    if attempt_started is None:
        # Nothing recorded when the attempt began, so the launch window a
        # fresh dispatch gets is the only launch clock there is: the pointer a
        # resume just rewrote, or the run's creation moment.
        launch = _stream_quiet_seconds(record, now_seconds=now_seconds)
    else:
        launch = max(0, int(now_seconds - attempt_started))
    return min(quiet, launch)


def _declared_wait_age_seconds(
    *,
    started_seconds: float,
    stream_mtime: float | None,
    now_seconds: float,
) -> int:
    """Age a declared wait, without counting time the run was writing output.

    A worker that is producing output is not parked, whatever its manifest
    declares. Measured 2026-09-18: a run was escalated from waiting to
    wait-aged at 1804 seconds while its newest stream file was zero minutes
    old and still growing, because the age was read from the declaration and
    nothing compared it against the run's own output. The clock that matters
    is therefore the later of the two, so a declaration sitting above a live
    stream stays young until the output actually stops — which is what makes
    wait-aged usable as a recovery trigger rather than a reading a coordinator
    has to take a second measurement to disbelieve.
    """
    latest = started_seconds
    if stream_mtime is not None and stream_mtime > latest:
        latest = stream_mtime
    return max(0, int(now_seconds - latest))


def _manifest_wait(
    manifest_data: Mapping[str, Any],
    manifest: Path,
    *,
    now_seconds: float,
    stale_after_seconds: int,
    stream_mtime: float | None = None,
    previous_lift: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the external-wait declaration a manifest actually holds.

    None means the manifest holds no wait — either it is not waiting at all, or
    it carries the four wait fields without a wait in them. A worker recording
    where it stands at orientation is not a parked run during an orientation:
    reading the mere presence of the fields as a declaration put healthy
    workers in the waiting column, aged them into wait-aged, and offered them
    to the resume sweep, which resumed them on a probe that was trivially
    true. A declaration whose condition names no wait, or whose probe cannot
    report a pending state, is therefore no declaration at all.

    A declaration that survives both readings still ages against the run's own
    output: a worker whose stream is still being written is producing, not
    parked, so the age of its wait is measured from the newer of the wait's
    declaration and the last stream write.

    A declaration whose terminal list names a state its own probe reports while
    the awaited job is still live is refused rather than honoured: it can never
    report a pending state, so it reads satisfied on every sweep and offers its
    run to the resume loop forever. The offending token is named in the refusal
    so the repair is a one-line edit rather than a reread of the probe.

    A declaration whose probe cannot fail is refused as invalid, with the
    probe named, and for the same reason: it names nothing outside the worker
    — ``echo pending``, ``git rev-parse HEAD`` — so it reports the same thing
    whatever the awaited work is doing and can neither end a wait nor report
    one still pending. The refusal names the probe rather than reducing the
    declaration to nothing, because the shape is one a worker can repair from
    the row.

    A condition takes one of two shapes. An argument vector is the one a
    scheduler query needs; ``wait_file`` is the ordinary one, because a worker
    waits for a job's log far more often than for a scheduler to report that
    the job left the queue. It names one path or an array of paths, and its
    probe and terminal are derived from those paths rather than declared, so
    the declaration reads as a wait in the same shape as any other. Two
    readers then answer it, and they are not one implementation: the sweep's
    reader in ``reckon/crew/resumption.py`` runs the derived vector and its
    ``exit:0`` sentinel and is the one that lifts a park, while
    ``_run_wait_condition_probe`` below looks for the paths directly and
    returns a row naming the ones still missing. Both must read the same
    declaration, and a case in ``tests/test_wait_shapes.py`` pins the lift
    through the sweep because a case that stops at the classifier's reader
    cannot show a run is ever resumed.

    A probe that is present but unreadable is refused by naming the shapes the
    reader does accept, and never reduced to the absence of a probe: a worker
    whose declaration was discarded reported to a coordinator as a worker that
    had declared nothing, which is a failure invisible at the moment it could
    still be repaired.

    ``previous_lift`` is the pointer's record of the last condition that lifted
    this run. A declaration identical to the one already lifted, arriving again
    after the worker re-parked, is the same condition reporting terminal a
    second time without ending: a wait-key defect, marked on the wait so the
    reader sees why the lift loop is stopped instead of watching it repeat.
    """
    if str(manifest_data.get("status") or "").strip().lower() != WAITING_STATUS:
        return None
    condition = str(manifest_data.get("wait_condition") or "").strip()
    declared_probe = _wait_probe(manifest_data.get("wait_probe"))
    files = _wait_file_paths(manifest_data.get("wait_file"))
    declared_terminal = _wait_terminal_values(manifest_data.get("wait_terminal"))
    if _wait_condition_declares_no_wait(condition):
        return None
    if _wait_probe_is_a_no_op(declared_probe):
        return None
    # A file condition's end is its paths' existence, so the terminal the
    # reader matches is derived rather than declared: one probe path answers
    # both shapes, and a file condition needs no exit-code sentinel written by
    # hand for the sweep that lifts it to read.
    terminal = declared_terminal or (["exit:0"] if files else [])
    probe = _wait_file_probe(files) if files else declared_probe
    resume_brief = str(manifest_data.get("resume_brief") or "").strip()
    missing = [
        name
        for name, value in (
            ("wait_condition", condition),
            ("wait_probe", probe),
            ("wait_terminal", terminal),
            ("resume_brief", resume_brief),
        )
        if not value
    ]
    # A shape the reader does not understand is refused by naming what it does
    # accept, so the declaration reaches the follower as something to repair
    # rather than as a worker that declared nothing.
    missing.extend(
        reason
        for reason in (
            _wait_probe_shape_refusal(manifest_data.get("wait_probe")),
            _wait_file_shape_refusal(manifest_data.get("wait_file")),
        )
        if reason
    )
    if files and declared_probe:
        missing.append(
            "either wait_probe or wait_file, not both: a wait has one shape"
        )
    if declared_probe and _wait_probe_cannot_fail(declared_probe):
        # A probe that runs but cannot differ is satisfied unconditionally, so
        # a wait resting on it reads the same however the awaited work is
        # doing. The refusal names the probe and the references the reader
        # accepts, because the repair is a one-line edit to the declaration.
        missing.append(
            f"wait_probe {[str(item) for item in declared_probe]!r} cannot "
            "fail: it names nothing outside the worker — no job id, pid, port "
            "or path — so its result cannot differ between sweeps and it tests "
            f"nothing; {_WAIT_ACCEPTED_SHAPES}"
        )
    if files and declared_terminal:
        missing.append(
            "wait_terminal alongside wait_file, where the condition ends when "
            "its paths exist"
        )
    unemitted = _wait_terminal_names_no_probe_state(declared_terminal)
    if unemitted:
        missing.append(
            f"wait_terminal listing {unemitted!r}, an exit-code sentinel "
            f"rather than a state the probe prints; {_WAIT_ACCEPTED_SHAPES}"
        )
    live_token = _wait_terminal_names_a_live_state(terminal, probe)
    if live_token:
        missing.append(
            f"wait_terminal listing {live_token!r}, a state the probe reports "
            "while the awaited job is still live"
        )
    started = None
    started_value = str(manifest_data.get("wait_started_at") or "").strip()
    if started_value:
        timestamp_value = _unquote_wait_declaration_scalar(started_value)
        started = parse_utc(timestamp_value)
        if started is None:
            missing.append("readable wait_started_at")
    if started is None:
        try:
            started_seconds = manifest.stat().st_mtime
        except OSError:
            started_seconds = now_seconds
    else:
        started_seconds = started.timestamp()
    age_seconds = _declared_wait_age_seconds(
        started_seconds=started_seconds,
        stream_mtime=stream_mtime,
        now_seconds=now_seconds,
    )
    expected_seconds, expected_error = _wait_expected_seconds(
        manifest_data, default_seconds=stale_after_seconds
    )
    if expected_error:
        missing.append(expected_error)
    signature = _wait_declaration_signature(condition, probe, terminal, resume_brief)
    wait_key_defect = ""
    if isinstance(previous_lift, Mapping) and previous_lift.get("trigger") == signature:
        wait_key_defect = (
            "wait-key defect: this declaration already lifted the run and has "
            "come back unchanged, so its terminal state "
            f"({', '.join(terminal) or 'unset'}) did not end the wait; the "
            "lift loop stays stopped until the declaration changes"
        )
    return {
        "condition": condition,
        "probe": probe,
        "terminal": terminal,
        "files": files,
        "resume_brief": resume_brief,
        "started_at": started_value
        or datetime.fromtimestamp(started_seconds, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "age_seconds": age_seconds,
        "expected_horizon_seconds": expected_seconds,
        "overdue": age_seconds > expected_seconds,
        "signature": signature,
        "valid": not missing,
        "error": "missing or invalid " + ", ".join(missing) if missing else "",
        "wait_key_defect": wait_key_defect,
    }


def external_wait(
    record: Mapping[str, Any],
    *,
    now_seconds: float | None = None,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
) -> dict[str, Any] | None:
    """Read a fresh external-wait declaration from one live pointer."""
    manifest = Path(str(record.get("manifest_path") or ""))
    _present, fresh = _run_chain_manifest_freshness(record)
    if not fresh:
        return None
    try:
        data = parse_manifest(manifest.read_text(encoding="utf-8"))
    except (OSError, ManifestParseError):
        return None
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    return _manifest_wait(
        data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
        stream_mtime=_run_stream_mtime(record),
        previous_lift=record.get("auto_resume"),
    )


def _reading_host() -> str:
    """The host this reader runs on, for gating process-table lookups."""
    return socket.gethostname()


def _launched_on_this_host(record: Mapping[str, Any]) -> bool:
    """Whether this host is the one that issued the record's pid.

    A pointer written before the launching host was recorded names none, and an
    unnamed host cannot be shown to be this one.
    """
    return (
        record.get("launcher_host") is not None
        and str(record.get("launcher_host")) == _reading_host()
    )


def _record_is_known_foreign(record: Mapping[str, Any]) -> bool:
    """Whether the record names a launching host that is a different machine.

    Distinct from :func:`_launched_on_this_host`, which also answers false for
    an unnamed host. A pointer with no launching host cannot be *shown* to be
    this host, but neither can it be shown to be another one, so a pid read that
    is safe to refuse on proof of a foreign machine is left to run on the mere
    absence of a name: an unnamed pointer predates the field, and its records
    are read as they always were rather than being refused for a host that was
    never written down.
    """
    host = record.get("launcher_host")
    return host is not None and str(host) != _reading_host()


def local_liveness(record: Mapping[str, Any]) -> tuple[bool | None, bool]:
    """The liveness this host can stand behind for one live pointer.

    Returns ``(alive, proven)``. The process table answers only when the
    record's launching host is this host: a pid is meaningful only on the
    machine that issued it, and the crew home is shared across login nodes, so
    asking a foreign process table — or carrying an answer taken there —
    fabricates a verdict in both directions. Where the launching host cannot be
    shown to be this host the stored answer is kept and ``proven`` is false,
    which is no evidence either way rather than proof of death, and every
    consumer that reads liveness from a pointer reads it here so one run cannot
    read two ways across the views that render it.

    A pointer pid that is gone does not yet end the work. The pointer names the
    supervisor for a supervised launch while the worker pid lives on the run
    directory's own worker record, and a supervisor that exits before its
    worker takes the pointer pid with it — the recorded pid then answers for a
    process that is gone while the work continues. The worker record is asked
    next, on the same terms: it carries no host of its own and numbers are
    reused across machines, so only a run launched here is read alive from it.

    The recorded end is not folded in here. A caller that emits an exit record
    reads it anyway, the reading above is what says whether the record applies,
    and a resumed attempt reuses the run directory, so an earlier attempt's
    record must not call the new worker dead.
    """
    launched_here = _launched_on_this_host(record)
    if launched_here and record.get("pid"):
        # The launched pid's kernel state is the authority at this instant, and
        # the recorded start tick rules out a reused pid. A zombie entry answers
        # not alive, composing with the narrowed probe rather than reviving an
        # older answer.
        alive = runs.record_process_alive(record)
        expected_start = record.get("pid_start_time")
        if alive is True and expected_start is not None:
            alive = _process_start_time(record.get("pid")) == expected_start
        proven = True
    else:
        alive = record.get("process_alive")
        proven = False
    if launched_here and alive is not True and _worker_record_liveness(record) is True:
        alive = True
        proven = True
    return alive, proven


def live_worker_pid(record: Mapping[str, Any]) -> int | None:
    """The pid this host observes still holding the run, or None.

    :func:`local_liveness` answers *whether* the run's process lives; a
    refusal that stops a resume over a living worker is checkable only if it
    also says *which* process, because the reader who receives it can then ask
    the process table the same question. This composes the same two reads in
    the same order — the pointer's pid, then the worker record the supervisor
    writes beside the stream, which is where a run whose supervisor exited
    ahead of its worker keeps the process that still runs it — and returns
    which of them answered. A pid is meaningful only on the machine that
    issued it, so a record that cannot be shown to be this host's names
    nothing here. None means no pid answered: either the run is not alive, or
    its liveness came from a stored answer that carries no process to name.
    """
    if not _launched_on_this_host(record):
        return None
    pointer_pid = _int_or_none(record.get("pid"))
    if pointer_pid is not None and runs.record_process_alive(record) is True:
        return pointer_pid
    worker_pid = _worker_record_pid(record)
    if worker_pid is not None and _worker_record_liveness(record) is True:
        return worker_pid
    return None


def _run_chain_manifest_freshness(record: Mapping[str, Any]) -> tuple[bool, bool]:
    """Judge delivery against the first dispatch across the attempt chain."""
    try:
        attempt = int(record.get("attempt") or 1)
        attempt_baseline = int(record["manifest_baseline_mtime_ns"])
    except (KeyError, TypeError, ValueError):
        return _manifest_freshness(record)
    first_dispatch = parse_utc(str(record.get("created_at") or ""))
    if first_dispatch is None:
        return _manifest_freshness(record)
    if attempt <= 1:
        return _manifest_freshness(record)
    first_dispatch_ns = (
        int(first_dispatch.timestamp()) * 1_000_000_000
        + first_dispatch.microsecond * 1_000
    )
    if attempt_baseline <= first_dispatch_ns:
        return _manifest_freshness(record)

    # Every attempt in one run shares the first dispatch as its time boundary.
    # A manifest written by any attempt is newer than that boundary and remains
    # readable as a handover, whatever status it carries. A manifest predating
    # the run stays older, so the freshness gate still rejects an unrelated
    # artifact instead of crediting it as this run's outcome. The resumed
    # attempt's own start time cannot serve here: the handover necessarily
    # predates the attempt that inherits it.
    chain_record = dict(record)
    chain_record["manifest_baseline_mtime_ns"] = first_dispatch_ns
    return _manifest_freshness(chain_record)


# The run directory's account of how a worker's process ended, written by the
# per-run supervisor in :mod:`reckon.crew.dispatch`. The supervisor is the only
# process holding the worker's parentage, so this file is where an exit is
# recorded even when the pointer is never updated again — and unlike a pid it
# stays meaningful on a machine that never launched the worker.
EXIT_RECORD_NAME = "exit.json"


def _run_exit_record(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The supervisor's exit record for a run, or None when there is none.

    Read verbatim and defensively: a file that cannot be read or parsed, or
    that names a different run, is absent rather than an error, so a damaged
    record classifies a run on its other evidence instead of refusing it.
    """
    run_id = str(record.get("run_id") or "")
    try:
        payload = json.loads(
            (_run_directory(record) / EXIT_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    recorded = str(payload.get("run_id") or "")
    if run_id and recorded and recorded != run_id:
        return None
    return dict(payload)


def _exit_record_end_phrase(exit_record: Mapping[str, Any]) -> str:
    """How the recorded process ended, in the record's own terms."""
    if exit_record.get("signal") is not None:
        name = exit_record.get("signal_name") or f"signal {exit_record['signal']}"
        return f"ended by {name}"
    exit_code = exit_record.get("exit_code")
    if exit_code is None:
        return "ended with no wait status recorded"
    return f"exited with code {exit_code}"


def _exit_record_is_launch_failure(exit_record: Mapping[str, Any]) -> bool:
    """Whether the record itself says the launch never reached a model."""
    if str(exit_record.get("ended_during") or "") == "launch":
        return True
    try:
        return int(exit_record.get("stream_records_seen")) == 0
    except (TypeError, ValueError):
        return False


def _interruption_evidence(
    record: Mapping[str, Any],
    *,
    phase: str,
    process_alive: bool | None,
    liveness_proven: bool,
    exit_record: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Return why unfinished work stopped involuntarily, plus retained commits.

    A recorded wait status or exit record is direct evidence that a signal
    ended the worker. Where no exit was recorded, death alone is ambiguous: an
    orphaned pointer already records that no terminal event arrived, while
    commits beyond the dispatch base prove an apparently working run left
    recoverable work behind. A deliberate stop or a recorded
    completion/promotion always outranks either inference, and a recorded exit
    outranks the death inferences: it is the end itself rather than a reading
    of a vanished pid, so a run that chose its exit is not an interruption.

    Retained work is inferred from the liveness reading alone, so the reading
    has to be one this host can stand behind: the crew home is shared across
    login nodes and a pid answers only on the host that issued it, so a stored
    answer from another observer is no reading here. Consumed without that
    qualification it lets a worker still running on its own machine read as an
    interruption of the work it is holding. An ending the run itself recorded is
    unaffected: it is read through the exit record, or through the phase an
    observer wrote beside the reading, and neither rests on the reading alone.
    """
    if phase in {"complete", "promoted", "stopped"} or record.get("promoted_at"):
        return None, 0

    signal_number = None
    signal_name = None
    signal_exit_code = None
    wait_status = record.get("wait_status")
    if isinstance(wait_status, Mapping) and wait_status.get("signal") is not None:
        signal_number = wait_status.get("signal")
        signal_name = str(wait_status.get("signal_name") or f"signal {signal_number}")
        signal_exit_code = wait_status.get("exit_code")
    elif exit_record is not None and exit_record.get("signal") is not None:
        # The pointer carries no wait status: that field is written by the
        # launcher holding the wait, and a supervisor-launched worker's exit
        # lands in the run directory instead. Same fact, recorded by the
        # process that collected it.
        signal_number = exit_record.get("signal")
        signal_name = str(exit_record.get("signal_name") or f"signal {signal_number}")
        signal_exit_code = exit_record.get("exit_code")
    if signal_number is not None:
        return (
            {
                "reason": "signal",
                "signal": signal_number,
                "signal_name": signal_name,
                "exit_code": signal_exit_code,
            },
            0,
        )

    if process_alive is not False:
        return None, 0
    if exit_record is not None:
        return None, 0
    if phase == "orphaned":
        return (
            {
                "reason": "dead-pid-no-exit",
                "signal": None,
                "signal_name": None,
                "exit_code": None,
            },
            0,
        )

    if not liveness_proven:
        return None, 0

    commits = _commits_beyond_base(record)
    if commits:
        return (
            {
                "reason": "dead-pid-with-retained-work",
                "signal": None,
                "signal_name": None,
                "exit_code": None,
            },
            commits,
        )
    return None, 0


def _seconds_since_dispatch(record: Mapping[str, Any], moment: float) -> float | None:
    """Seconds between the run's dispatch and ``moment``, or None when unknown.

    The dispatch is the pointer's ``created_at``, recorded by the launcher. A
    missing or unparseable stamp is None rather than zero: a run whose launch
    time cannot be read has taken no measurement, and a zero would place every
    such run outside the launch window on no evidence.
    """
    dispatched = parse_utc(str(record.get("created_at") or ""))
    if dispatched is None:
        return None
    return moment - dispatched.timestamp()


def _within_launch_window(record: Mapping[str, Any], moment: float) -> bool:
    """Whether the run is still inside the window after its own dispatch.

    A negative elapsed time — a clock that moved backwards, or a caller passing
    a moment before the launch — is not inside the window: the guard covers the
    run's own first minutes and nothing a future time is asked to invent.
    """
    elapsed = _seconds_since_dispatch(record, moment)
    return elapsed is not None and 0 <= elapsed < LAUNCH_WINDOW_SECONDS


def _manifest_may_be_mid_rewrite(
    record: Mapping[str, Any], manifest: Path, manifest_error: str, moment: float
) -> bool:
    """Whether a manifest that cannot be parsed is plausibly being rewritten.

    Workers write manifests in place rather than atomically, so a reader can
    catch a file between the truncate and the write: it is unparseable, and it
    either moved seconds ago or is smaller than the size the last readable read
    saw. Both are the absence of a verdict in transit, not an absence of
    delivery, so the reader treats the file as unchanged rather than reading its
    contents as a refusal. Only a parse failure qualifies — a readable manifest
    is a verdict whatever the writer would do next.

    Both signatures are bounded by the same short window, and neither can hold a
    reading back past it. The mtime signature is measured from the file's own
    modification; the size signature is measured from the read that recorded the
    larger size. Without that second bound a path that once held a bigger
    readable manifest would suppress the unwritten reading for as long as it
    stayed unreadable and smaller, which is exactly the shape of a retry reusing
    its run directory's manifest, and the run would never be reported at all.
    """
    if not manifest_error:
        return False
    try:
        stat = manifest.stat()
    except OSError:
        return False
    if moment - stat.st_mtime < MANIFEST_REWRITE_WINDOW_SECONDS:
        return True
    remembered = _MANIFEST_SIZES_READ.get(_manifest_size_key(record, manifest))
    if remembered is None:
        return False
    size, recorded_at = remembered
    return (
        moment - recorded_at < MANIFEST_REWRITE_WINDOW_SECONDS and stat.st_size < size
    )


def _manifest_size_key(record: Mapping[str, Any], manifest: Path) -> str:
    """The identity of one run's view of one manifest path.

    A retry writes the same path, so the path alone would let it inherit the
    size a predecessor left behind and suppress its own reading. The key carries
    the run and the attempt, and falls back to the dispatch stamp for a pointer
    that records no attempt, so a redispatch starts with nothing remembered.
    """
    run_id = str(record.get("run_id") or "")
    attempt = record.get("attempt")
    if attempt is None:
        attempt = str(record.get("created_at") or "")
    return f"{run_id}::{attempt}::{manifest}"


def _remember_manifest_size(key: str, size: int, moment: float) -> None:
    """Record the last readable size of a manifest under its run's identity.

    A long-lived reader classifies every run it sees, so the memory is capped
    and evicts the least recently touched entry. The map's own bound is the
    policy: holding entries only for runs that currently hold a live pointer
    would tie this cache to a fleet read it does not otherwise need, and would
    drop the entry for a run whose pointer is briefly unreadable.
    """
    _MANIFEST_SIZES_READ.pop(key, None)
    _MANIFEST_SIZES_READ[key] = (size, moment)
    while len(_MANIFEST_SIZES_READ) > MANIFEST_SIZE_MEMORY_MAX:
        oldest = next(iter(_MANIFEST_SIZES_READ))
        del _MANIFEST_SIZES_READ[oldest]


def _absence_of_a_verdict_is_transient(
    record: Mapping[str, Any],
    manifest: Path,
    manifest_error: str,
    moment: float,
) -> bool:
    """Whether a live run's missing written verdict is too early to report.

    Two windows cover it: the launch window after dispatch, before the worker
    has had time to write anything, and a manifest caught mid-rewrite. In both
    the run has not failed to deliver — it has not finished writing — so the
    caller keeps its liveness reading instead of naming it unwritten.
    """
    return _within_launch_window(record, moment) or _manifest_may_be_mid_rewrite(
        record, manifest, manifest_error, moment
    )


from .recovery_vocabulary import (  # noqa: E402
    LAUNCH_WINDOW_SECONDS,
    MANIFEST_REWRITE_WINDOW_SECONDS,
    WAITING_STATUS,
)
from .recovery_wait import (  # noqa: E402
    _WAIT_ACCEPTED_SHAPES,
    _newest_stream_shows_work,
    _run_directory,
    _unquote_wait_declaration_scalar,
    _wait_condition_declares_no_wait,
    _wait_declaration_signature,
    _wait_expected_seconds,
    _wait_file_paths,
    _wait_file_probe,
    _wait_file_shape_refusal,
    _wait_probe,
    _wait_probe_cannot_fail,
    _wait_probe_is_a_no_op,
    _wait_probe_shape_refusal,
    _wait_terminal_names_a_live_state,
    _wait_terminal_names_no_probe_state,
    _wait_terminal_values,
    newest_stream,
)
from .recovery_watch import (  # noqa: E402
    _commits_beyond_base,
    _utc_seconds,
)
