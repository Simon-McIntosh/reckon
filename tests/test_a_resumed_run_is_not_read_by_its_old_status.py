"""A resumed run is not read by its previous attempt's status.

``crew resume`` reuses a run's directory and the manifest a previous turn left,
and the reset before a resume clears only the supervisor's done words
(``complete`` and ``failed``): a ``blocked``, ``waiting`` or ``in-progress``
status stays on the file. Read from that word alone the resumed run is drawn as
its predecessor's outcome, so a live attempt reads blocked and the stop hook
raises a duty to resolve a blocker the resume already resolved. The attempt's
own clock separates the two — a manifest last written before the current attempt
began cannot be that attempt's report — and ``_attempt_started_seconds`` in
``reckon/crew/recovery_liveness.py`` reads the later of the pointer's and the
attempt record's start, so a resumed attempt is dated from its own beginning.

Measured 2026-10-15: run ``r-20261015T183541384225-a-claimless-review-is-refused-at-store``
stopped as ``blocked`` with a manifest written at 19:31:06Z; the coordinator
widened its scope and resumed it, and attempt 2 started at 19:44:46Z working —
yet the stop hook still listed the run ``[blocked]`` and told the coordinator to
resolve a blocker that no longer stood.

The producer of the reported row sits on another host, so the predecessor's pid
is not its to look up: liveness reads unknown, and the run's current attempt is
known to be moving only from its own clock. That is the shape every case here
builds — a foreign launcher host, a quiet predecessor stream, and an attempt
record dating the current attempt's own start.

The declared negative control removes the attempt-start comparison, and the
first case must then read ``blocked``.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, recovery_liveness, runs
from tests.helpers.git import git as _git

classification_module = importlib.import_module("reckon.crew.recovery_classification")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "resumed-status-fixture"
SESSION = "coordinator-fixture"

# A pid nothing runs under, for the predecessor's own process.
DEAD_PID = 99999999

# A launching host that is provably not this one, so no pid is probed here.
FOREIGN_HOST = "some-other-host"

# The declared mutation, verbatim: the string a promotion audit compares the red
# log against.
DECLARED_MUTATION = (
    "with the attempt-start comparison removed, the factory's first case must "
    "read blocked"
)

MUTATION_ENV = "RECKON_RESUMED_RUN_STATUS_NEGATIVE_CONTROL"

# The no-clock isolation control: makes the comparison answer from a clock that
# is always there, so a case built with no recorded attempt start is dated
# anyway and must stop holding.
NO_CLOCK_CONTROL_ENV = "RECKON_RESUMED_RUN_STATUS_NO_CLOCK_CONTROL"

# Ages, in seconds: the predecessor's manifest and stream are old — the stream
# quiet past the stall window, so liveness reads unknown and the run is not
# already moving on the stream alone — and the current attempt is young.
STALE_SECONDS = 1200.0
ATTEMPT_STARTED_SECONDS = 30.0

# The predecessor's stream: a turn-opened record and no assistant turn, so a
# resumed run whose attempt is young is not already moving on the stream alone —
# the reported row's shape, where the old status word is what reached the reader.
STREAM_RECORDS = ['{"type":"turn.started"}']


def _no_attempt_comparison(*_args: Any, **_kwargs: Any) -> bool:
    """The declared mutation: the attempt-start comparison is gone."""
    return False


def _a_clock_that_is_always_there(_record: Any, manifest: Any) -> bool:
    """The no-clock control: date the attempt from the wall clock instead.

    A run that recorded no attempt start has no clock the comparison can read,
    so the case asserting the old word stands passes for that reason alone.
    Answering from ``time.time()`` — a clock that exists for every run — gives
    even that run an attempt start, and the case must then fail.
    """
    path = Path(str(manifest))
    try:
        written = path.stat().st_mtime
    except OSError:
        return False
    return written < time.time()


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary, resolved crew home; the operator's is never read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("commit", "-q", "--allow-empty", "-m", "test: seed resumed status fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    if os.environ.get(MUTATION_ENV) == "1":
        # The declared mutation: the attempt-start comparison never fires, so a
        # resumed run is read from its predecessor's word again.
        monkeypatch.setattr(
            classification_module,
            "_manifest_predates_the_current_attempt",
            _no_attempt_comparison,
        )
    if os.environ.get(NO_CLOCK_CONTROL_ENV) == "1":
        # The no-clock isolation control: the comparison answers from a clock
        # every run has, so a run recording no attempt start is dated anyway.
        monkeypatch.setattr(
            classification_module,
            "_manifest_predates_the_current_attempt",
            _a_clock_that_is_always_there,
        )
    return config_home


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _wait_declaration_lines() -> str:
    """A valid external-wait declaration, so a ``waiting`` word reads as a wait."""
    return (
        "wait_condition: the run's own job 4242\n"
        'wait_probe: ["sacct", "-n", "-X", "-o", "State", "-j", "4242"]\n'
        'wait_terminal: ["COMPLETED"]\n'
        "resume_brief: continue once the job has completed\n"
    )


def _write_run(
    *,
    run_id: str,
    status: str,
    manifest_age_seconds: float,
    attempt_start_age_seconds: float | None,
    wait_declaration: bool = False,
    launcher_host: str | None = FOREIGN_HOST,
    worker_launch_age_seconds: float | None = ATTEMPT_STARTED_SECONDS,
    attempt_record_undated: bool = False,
    stream_age_seconds: float = STALE_SECONDS,
) -> dict:
    """Write one run's manifest, attempt record, worker record, stream and pointer.

    The manifest carries a status the reset leaves in force, written
    ``manifest_age_seconds`` ago. The attempt record names the current attempt's
    start, later than the manifest when the resume has happened. The pointer
    names a foreign launching host, so liveness reads unknown here — the
    cross-host producer of the reported row.

    Both clocks the attempt-start comparison can read are optional, so a case
    can build a run with no recorded attempt start at all: pass
    ``attempt_start_age_seconds=None`` for no attempt record and
    ``worker_launch_age_seconds=None`` for a worker record that names no launch.
    """
    now = time.time()
    run_directory = runs.run_dir(run_id)
    run_directory.mkdir(parents=True, exist_ok=True)
    manifest = run_directory / "manifest.md"
    body = f"node: {run_id}\nstatus: {status}\n"
    if wait_declaration:
        body += _wait_declaration_lines()
    manifest.write_text(body, encoding="utf-8")
    written = now - manifest_age_seconds
    os.utime(manifest, (written, written))

    stream = run_directory / "attempt-1.jsonl"
    stream.write_text("\n".join(STREAM_RECORDS) + "\n", encoding="utf-8")
    # The stream is the predecessor's, dated from before the resume by default,
    # so an attempt is known to be moving only from its own clock.
    quiet = now - stream_age_seconds
    os.utime(stream, (quiet, quiet))

    if attempt_start_age_seconds is not None:
        attempt_record = {
            "run_id": run_id,
            "attempt": 2,
            "attempt_started_at": _at(now - attempt_start_age_seconds),
        }
    elif attempt_record_undated:
        # The run has published its current attempt's identity and recorded no
        # start for it, so the identity gate passes and there is still no clock
        # for the comparison to read.
        attempt_record = {"run_id": run_id, "attempt": 2}
    else:
        attempt_record = None
    if attempt_record is not None:
        (run_directory / recovery_liveness.ATTEMPT_RECORD_NAME).write_text(
            json.dumps(attempt_record), encoding="utf-8"
        )

    worker_record = {
        "run_id": run_id,
        "attempt": 2,
        "pid": DEAD_PID,
        "pid_start_time": recovery._process_start_time(DEAD_PID),
    }
    if worker_launch_age_seconds is not None:
        worker_record["launched_at"] = _at(now - worker_launch_age_seconds)
    (run_directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps(worker_record), encoding="utf-8"
    )

    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
        "phase": "working",
        "created_at": _at(now - manifest_age_seconds - 60),
        "attempt": 2,
        "attempt_kind": "resume",
        "pid": DEAD_PID,
        "launcher_host": launcher_host,
        "process_alive": None,
        "manifest_path": str(manifest),
        "log_path": str(stream),
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _classify(pointer: dict) -> dict:
    # A condition probe is supplied so the classification never runs sacct: the
    # waiting word's observation is not the subject of any case here.
    return recovery.classify_pointer(
        pointer,
        now_seconds=time.time(),
        condition_test=lambda _record, _wait: "RUNNING",
    )


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def _stub_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 0}
    )


def test_a_blocked_manifest_predating_the_current_attempt_reads_working() -> None:
    """The first case: the predecessor's ``blocked`` is not this attempt's status.

    The manifest was written ten minutes ago; the attempt record dates the
    current attempt from thirty seconds ago, so the run is moving on its own
    clock even though liveness reads unknown here. Read from its own attempt the
    run has delivered no verdict yet, so it reads working.
    """
    run_id = "run-superseded-blocked"
    pointer = _write_run(
        run_id=run_id,
        status="blocked",
        manifest_age_seconds=STALE_SECONDS,
        attempt_start_age_seconds=ATTEMPT_STARTED_SECONDS,
    )

    row = _classify(pointer)

    assert row["classification"] == "running", row["detail"]
    assert row["recovery_classification"] != "blocked", row
    assert row["manifest_status"] is None, row["manifest_status"]
    snapshot = _snapshot(pointer)
    assert snapshot["state"] in recovery.FLEET_WORKING_STATES, snapshot


def test_a_blocked_manifest_rewritten_after_the_attempt_started_reads_blocked() -> None:
    """The old word governs again once the new worker rewrites the manifest.

    The manifest's modification time is later than the attempt's start, so it is
    this attempt's own report, and it reads blocked exactly as before.
    """
    run_id = "run-current-blocked"
    pointer = _write_run(
        run_id=run_id,
        status="blocked",
        manifest_age_seconds=5.0,
        attempt_start_age_seconds=ATTEMPT_STARTED_SECONDS,
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked", row["detail"]
    assert row["manifest_status"] == "blocked", row["manifest_status"]


def test_a_run_with_no_recorded_attempt_start_reads_as_today() -> None:
    """No attempt clock, so nothing separates the word from the attempt.

    The run has published its current attempt's identity — the attempt record
    names attempt 2, so the rule's first gate passes — and recorded no start for
    it: the attempt record carries no start time and the worker record names no
    launch. Nothing anywhere dates the attempt, so the comparison cannot reach
    the blocked word and it stands, exactly as it did before the rule existed.
    The case holds only because that clock is absent, which the no-clock control
    in this module demonstrates: with a clock supplied anyway, the same run reads
    working and this assertion fails. Its stream is recent, so the rule's
    live-or-moving fact is satisfied and the missing clock is the only thing
    standing the comparison down.
    """
    run_id = "run-no-attempt-start"
    pointer = _write_run(
        run_id=run_id,
        status="blocked",
        manifest_age_seconds=5.0,
        attempt_start_age_seconds=None,
        worker_launch_age_seconds=None,
        attempt_record_undated=True,
        stream_age_seconds=5.0,
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked", row["detail"]
    assert row["manifest_status"] == "blocked", row["manifest_status"]


@pytest.mark.parametrize("status", ["blocked", "waiting"])
def test_every_non_terminal_word_the_reset_leaves_reads_working(status: str) -> None:
    """The rule covers blocked, waiting and in-progress, not blocked alone.

    ``waiting`` carries its declaration so the base reading is a genuine wait
    rather than a word that already fell through to liveness.
    """
    run_id = f"run-superseded-{status}"
    pointer = _write_run(
        run_id=run_id,
        status=status,
        manifest_age_seconds=STALE_SECONDS,
        attempt_start_age_seconds=ATTEMPT_STARTED_SECONDS,
        wait_declaration=(status == "waiting"),
    )

    row = _classify(pointer)

    assert row["classification"] == "running", row["detail"]
    assert row["manifest_present"] is False, row


def test_a_word_that_already_means_no_verdict_is_left_alone() -> None:
    """An ``in-progress`` word carries no verdict to withdraw.

    It is the word the run reads as while it works, so the reading is already
    the attempt's own, and dropping it would read a resumed launch as a run
    abandoned without a manifest.
    """
    run_id = "run-superseded-progress"
    pointer = _write_run(
        run_id=run_id,
        status="in-progress",
        manifest_age_seconds=STALE_SECONDS,
        attempt_start_age_seconds=ATTEMPT_STARTED_SECONDS,
    )

    row = _classify(pointer)

    assert row["classification"] == "running", row["detail"]
    assert row["manifest_present"] is True, row["manifest_present"]


def test_a_resumed_run_naming_the_predecessors_dead_pid_reads_working() -> None:
    """The pointer's dead pid belongs to the attempt the resume replaced.

    The run is read here, so the process table answers for the pid the pointer
    still carries — and that pid is attempt 1's, so its absence is that
    attempt's end and not this one's. The attempt record dates attempt 2 after
    the manifest, so attempt 1's ``blocked`` word is not this attempt's status
    and the run must not read blocked.
    """
    run_id = "run-dead-predecessor-pid"
    pointer = _write_run(
        run_id=run_id,
        status="blocked",
        manifest_age_seconds=STALE_SECONDS,
        attempt_start_age_seconds=ATTEMPT_STARTED_SECONDS,
        launcher_host=socket.gethostname(),
    )

    row = _classify(pointer)

    assert row["classification"] == "running", row["detail"]
    assert row["recovery_classification"] != "blocked", row
    assert row["manifest_present"] is False, row


def test_a_superseded_blocked_run_derives_no_blocked_duty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop hook raises no obligation to resolve a blocker already resolved."""
    run_id = "run-superseded-obligation"
    _write_run(
        run_id=run_id,
        status="blocked",
        manifest_age_seconds=STALE_SECONDS,
        attempt_start_age_seconds=ATTEMPT_STARTED_SECONDS,
    )
    _stub_drain(monkeypatch)

    result = obligations_module.obligations(PROJECT, SESSION)

    owed = [
        item
        for item in result["obligations"]
        if item["run_id"] == run_id and item["kind"] == "blocked"
    ]
    assert owed == []
