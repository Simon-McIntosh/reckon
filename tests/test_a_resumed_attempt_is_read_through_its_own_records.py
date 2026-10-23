"""A resumed attempt is read through its own records, not its predecessor's.

A resume takes the attempt claim ``attempt-N.claim`` before it does anything
else, and only afterwards does it publish the attempt record, spawn the worker
and write the worker record. Between the claim and the attempt record the run
directory holds the claim and nothing naming attempt N, while the predecessor's
exit record and the coordinator's repaired ``blocked`` manifest are both still
in place. The predecessor-report guard resolves the current attempt from the
attempt record alone, so in that window it has no attempt number to compare
against, stands down, and lets the repaired word govern the attempt that is
only starting — the run is drawn halted and a reader is sent to a blocker the
resume already dealt with.

The claim is the run's own statement that an attempt began, and it carries the
moment it began, so it is the record that dates a resumed attempt while the rest
of its own evidence is still being written. The guard reads it here. What keeps
that from swallowing a real halt is the end: an attempt that has recorded an end
of its own is read from it, so an exit record naming attempt N stands the guard
down and the word it wrote governs, as it must.

The recorded case is ``r-20261023T012524464845-a-plan-write-refuses-the-bucket``:
a status repair at 12:14:30Z, a resume claim at 12:14:49Z, attempt 4 launched at
12:15:11Z, and the row at 12:25:51Z still reading the repaired word.

The declared negative control restores the base guard, and arms i to iv must
then draw halted.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_liveness, runs
from reckon.crew.ticker import PANE_STATES
from tests import test_a_live_run_never_reads_dead as liveness

classification_module = importlib.import_module("reckon.crew.recovery_classification")

# The declared mutation, verbatim: the string a promotion audit compares the red
# log against.
DECLARED_MUTATION = (
    "restore the base revision's predecessor-report guard so the repaired "
    "blocked status governs the resumed attempt; arms i to iv must then draw "
    "halted"
)

MUTATION_ENV = "RECKON_RESUMED_ATTEMPT_CLAIM_NEGATIVE_CONTROL"

# The words the pane draws halted, taken from the pane's own vocabulary.
HALTED_STATES = PANE_STATES["halted"]

# A pid nothing runs under, for the predecessor's own process.
DEAD_PID = 99999999

# A launching host that is provably not this one, so no pid is probed here.
FOREIGN_HOST = "some-other-host"

# The repaired manifest and the predecessor's stream are old — quiet past the
# stall window — so the run is not already moving on the stream alone.
STALE_SECONDS = 1200.0


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The crew home is a temporary directory; the operator's is never read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    if os.environ.get(MUTATION_ENV) == "1":
        # The declared mutation: the guard resolves the current attempt from the
        # attempt record alone, so a claim-only resume stands it down and the
        # predecessor's blocked word governs again.
        monkeypatch.setattr(
            classification_module,
            "_current_attempt_number",
            _attempt_record_number_only,
        )
    return config_home


def _attempt_record_number_only(record: dict) -> int | None:
    """The base reading: the attempt number, from the attempt record alone.

    The claim is not consulted, so a resume that has taken its claim and not yet
    published its attempt record resolves to no attempt at all — which is the
    window the recorded case sat in.
    """
    try:
        marker = json.loads(
            (
                recovery_liveness._run_directory(record)
                / recovery_liveness.ATTEMPT_RECORD_NAME
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    return recovery_liveness._int_or_none(marker.get("attempt"))


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _write_run(
    *,
    run_id: str,
    attempt: int,
    claim_age_seconds: float,
    manifest_status: str = "blocked",
    manifest_age_seconds: float = STALE_SECONDS,
    status_repair: bool = False,
    repair_age_seconds: float | None = None,
    exit_attempt: int | None = None,
    exit_age_seconds: float = 300.0,
    exit_ended_during: str = "working",
    exit_stream_records: int = 5,
    worker_record: bool = False,
    stream_age_seconds: float = STALE_SECONDS,
    attempt_record: bool = False,
    local_launcher: bool = False,
) -> dict:
    """Write one resumed run's own records and return its pointer.

    The claim is the resume's first write and carries the moment the attempt
    began. Everything a later attempt records — the attempt record, the worker
    record — is optional, so a case can build the window the claim alone fills.
    ``attempt_record`` publishes attempt.json for the arms where the resume has
    got that far. ``local_launcher`` names this host as the launching host, so
    the pointer's pid is probed here and reads as gone; left false the launcher
    is another host and the pid is unprobeable, which is the reading a producer
    on a peer host makes.
    """
    now = time.time()
    pid = liveness._absent_pid() if local_launcher else DEAD_PID
    directory = directory_of(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: {manifest_status}\ncommits:\nblockers:\n",
        encoding="utf-8",
    )
    written = now - manifest_age_seconds
    os.utime(manifest, (written, written))

    claim = directory / f"attempt-{attempt}.claim"
    claim.write_text(
        json.dumps(
            {
                "attempt": attempt,
                "pid": pid,
                "pid_start_time": "1",
                "at": _at(now - claim_age_seconds),
            }
        ),
        encoding="utf-8",
    )

    if status_repair:
        (directory / "status-repair.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "manifest": str(manifest),
                    "status": manifest_status,
                    "previous_status": "complete",
                    "reason": "one folded finding is answered in a short round",
                    "repaired_at": _at(
                        now
                        - (
                            repair_age_seconds
                            if repair_age_seconds is not None
                            else manifest_age_seconds
                        )
                    ),
                }
            ),
            encoding="utf-8",
        )

    if attempt_record:
        (directory / recovery_liveness.ATTEMPT_RECORD_NAME).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": attempt,
                    "attempt_kind": "resume",
                    "attempt_started_at": _at(now - claim_age_seconds),
                }
            ),
            encoding="utf-8",
        )

    if worker_record:
        body = json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "pid": pid,
                "pid_start_time": "1",
                "launched_at": _at(now - 25.0),
            }
        )
        (directory / recovery.WORKER_RECORD_NAME).write_text(body, encoding="utf-8")
        (directory / f"attempt-{attempt}-worker.json").write_text(
            body, encoding="utf-8"
        )

    if exit_attempt is not None:
        exit_path = directory / recovery.EXIT_RECORD_NAME
        exit_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": exit_attempt,
                    "worker_pid": None,
                    "ended_during": exit_ended_during,
                    "exit_code": 0,
                    "launched_at": _at(now - exit_age_seconds),
                    "exited_at": _at(now - exit_age_seconds + 60),
                    "stream_records_seen": exit_stream_records,
                }
            ),
            encoding="utf-8",
        )
        stamp = now - exit_age_seconds
        os.utime(exit_path, (stamp, stamp))

    stream = directory / f"resume-{attempt}.jsonl"
    stream.write_text(
        '{"type":"assistant","message":{"content":[]}}\n', encoding="utf-8"
    )
    quiet = now - stream_age_seconds
    os.utime(stream, (quiet, quiet))

    pointer = {
        "run_id": run_id,
        "project": "resumed-claim-fixture",
        "session": "coordinator-fixture",
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": ["seed.txt"],
        },
        "phase": "working",
        "attempt": attempt - 1,
        "attempt_kind": "resume",
        "pid": pid,
        "launcher_host": socket.gethostname() if local_launcher else FOREIGN_HOST,
        "process_alive": None,
        "manifest_path": str(manifest),
        "log_path": str(stream),
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def directory_of(run_id: str) -> Path:
    """The run's directory, through the same resolver the reader uses."""
    return runs.run_dir(run_id)


def _classify(pointer: dict) -> dict:
    return recovery.classify_pointer(
        pointer,
        now_seconds=time.time(),
        condition_test=lambda _record, _wait: "RUNNING",
    )


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def _assert_state(pointer: dict, arm: str, expected: str) -> dict:
    """The arm's claim: the pane word, asserted by name and not by membership.

    The word is asserted exactly rather than as membership of the working set,
    so a run that reads a neighbouring working state fails here instead of
    passing quietly. The claim-only arms read the pane's working word: the
    classifier stands the predecessor's word down and leaves the resumed
    attempt's liveness unknown, which is the reading the pane draws from the
    pointer's phase. The run's own reading — that it awaits a worker it has not
    registered — is asserted beside it by ``_assert_reads_as_starting``.
    """
    snapshot = _snapshot(pointer)
    state = snapshot["state"]
    assert state not in HALTED_STATES, (
        f"{arm}: the repaired word governs the attempt that is only starting; "
        f"it read {state!r} with {snapshot!r}"
    )
    assert state == expected, (arm, state, snapshot)
    return pointer


def _assert_reads_as_starting(pointer: dict, arm: str) -> None:
    """The attempt's own reading: it awaits the worker it has not registered.

    The current attempt's record is a claim and nothing more, so the run is read
    as an attempt that is only starting rather than as the end of the attempt
    the pointer still names. ``_attempt_awaits_its_worker`` is the reading that
    states it, and the classification says so in its own clause.
    """
    assert recovery_liveness._attempt_awaits_its_worker(pointer) is True, (
        f"{arm}: the current attempt has registered no worker of its own, so it "
        "awaits one"
    )
    detail = str(_classify(pointer).get("detail") or "")
    assert "no process and no end of its own" in detail, (arm, detail)


def test_arm_i_a_claim_after_a_status_repair_is_not_the_repaired_word() -> None:
    """Arm i: the repair is older than the claim, so the claim is the later fact.

    The coordinator repaired the manifest to ``blocked`` at 12:14:30Z and the
    resume took its claim at 12:14:49Z. The repaired manifest is written before
    the attempt began, so it is not that attempt's report, and the attempt has
    recorded no end of its own.
    """
    run_id = "run-claim-after-repair"
    pointer = _write_run(
        run_id=run_id,
        attempt=2,
        claim_age_seconds=350.0,
        status_repair=True,
        local_launcher=True,
    )

    _assert_state(pointer, "arm i", "working")
    _assert_reads_as_starting(pointer, "arm i")


def test_arm_ii_a_claiming_attempt_with_a_worker_is_not_the_repaired_word() -> None:
    """Arm ii: the same run one step later, with a worker record of its own.

    The worker record names attempt 2, so the run has a process of its own to be
    read from, and its stream is moving. Neither fact reaches the reading while
    the predecessor's word is allowed to govern the attempt.
    """
    run_id = "run-claim-with-worker"
    pointer = _write_run(
        run_id=run_id,
        attempt=2,
        claim_age_seconds=350.0,
        status_repair=True,
        worker_record=True,
        stream_age_seconds=20.0,
    )

    _assert_state(pointer, "arm ii", "working")
    assert recovery_liveness._attempt_awaits_its_worker(pointer) is False, (
        "arm ii: the current attempt has registered a worker of its own, so it "
        "no longer awaits one"
    )


def test_arm_iii_a_launch_failed_attempt_with_a_next_claim_starts() -> None:
    """Arm iii: attempt 1 failed to launch, and attempt 2 has claimed.

    The exit record names attempt 1 and says it ended while launching, with no
    stream records of its own. The next attempt's claim is the run's latest
    statement about itself, and the failed launch is not its end.
    """
    run_id = "run-launch-failed-then-claimed"
    pointer = _write_run(
        run_id=run_id,
        attempt=2,
        claim_age_seconds=30.0,
        exit_attempt=1,
        exit_ended_during="launch",
        exit_stream_records=0,
        local_launcher=True,
    )

    _assert_state(pointer, "arm iii", "working")
    _assert_reads_as_starting(pointer, "arm iii")
    assert pointer["attempt"] == 1, pointer


def test_arm_iv_an_exit_unfinished_attempt_with_a_next_claim_starts() -> None:
    """Arm iv: attempt 4 exited unfinished, and attempt 5 has claimed.

    Attempt 4's end is a real halt that the resume answered by starting attempt
    5. Read from attempt 4's exit record alone the run reads as the attempt that
    already ended, so the claim is what dates the reading.
    """
    run_id = "run-exit-unfinished-then-claimed"
    pointer = _write_run(
        run_id=run_id,
        attempt=5,
        claim_age_seconds=30.0,
        exit_attempt=4,
        local_launcher=True,
    )

    _assert_state(pointer, "arm iv", "working")
    _assert_reads_as_starting(pointer, "arm iv")
    assert pointer["attempt"] == 4, pointer


def test_the_current_attempts_own_end_still_governs() -> None:
    """The negative control's shape: attempt 2's own exit is written after the repair.

    An attempt that has recorded its own end has a verdict of its own, and no
    claim after it can take that away. The exit record names attempt 2, the
    attempt the claim and the repaired manifest both describe, so the run reads
    halted
    from the word the resume left in place.
    """
    run_id = "run-claim-with-own-exit"
    pointer = _write_run(
        run_id=run_id,
        attempt=2,
        claim_age_seconds=350.0,
        status_repair=True,
        exit_attempt=2,
        exit_age_seconds=300.0,
    )

    snapshot = _snapshot(pointer)

    assert snapshot["state"] in HALTED_STATES, (
        "the attempt recorded its own end, so the repaired word governs; "
        f"it read {snapshot['state']!r} with {snapshot!r}"
    )


def test_a_claim_alone_dates_the_attempt() -> None:
    """The claim is a clock the attempt-start comparison can read.

    Nothing else records when attempt 2 began — no pointer field, no attempt
    record — so the comparison has no attempt start unless it reads the claim.
    The manifest is written before it, so it cannot be this attempt's report.
    """
    run_id = "run-claim-is-the-clock"
    pointer = _write_run(
        run_id=run_id,
        attempt=2,
        claim_age_seconds=120.0,
    )

    assert (
        recovery_liveness._manifest_predates_the_current_attempt(
            pointer, Path(pointer["manifest_path"])
        )
        is True
    )
