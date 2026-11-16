"""A resumed repair is not killed as a run its earlier attempt delivered.

A repair round is started by resuming a run that has already delivered: its
pointer carries the ``complete`` the earlier attempt's delivery wrote, and the
manifest is the in-progress one the resume left. Every other writer of that
phase restores what it read before the resume — a promotion drain writes back
the phase its request found, a fold reads the superseded attempt's stream — so
the live repair's pointer can read ``complete`` again at any moment during its
work.

The terminal-run guard read that word as the attempt's own delivery and ended
the worker. Measured on 15 November: eight resumed attempts hold an exit record
reading ``ended_during: working`` with a forced exit whose phase is ``complete``
and whose reason is ``terminal-run``. The guard now reads the exit receipt the
attempt's own ending writes. The receipt belongs to one attempt; the word
belongs to whoever wrote it last.

The resume also answers the stale word: while the worker is live in the
supervisor's own charge and the attempt holds no exit receipt, the pointer reads
``working`` again rather than as a finished attempt with a live process.

The cases drive the supervisor in this process so its worker can be observed,
and the stale word is written by the driver after the resume has recorded its
attempt — the restore the real writers perform. Each case names a door because
all three doors (a folded finding, a hand resume, the reflex sweep) call
``_resume``; the guard sees one attempt either way, and the two doors that are
cheap to drive stand for the set.

The declared mutation makes the word authoritative again — the receipt check is
answered ``True`` without reading it — and the first case then finds the stub
worker signalled. The last two cases hold the other directions: an attempt that
really did deliver still ends inside the grace, and an attempt whose receipt
says it ended still reads as terminal.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_launch as dispatch_launch_module
import reckon.crew.resumption as resumption_module
from reckon import _backends
from reckon.crew import runs
from reckon.crew_run_commands import crew_resume

# A stub worker that works and never delivers: it leaves the manifest the resume
# wrote it and sleeps past any grace, so an exit inside the bound can only be
# the supervisor's act.
STUB_WORKING = "import time\nwhile True:\n    time.sleep(0.05)\n"

# A stub worker that delivers its own verdict and then sleeps, so the ordinary
# ending is exercised on an attempt that really did deliver.
STUB_DELIVERS = (
    "import os, time\n"
    "from pathlib import Path\n"
    "Path(os.environ['RECKON_MANIFEST']).write_text(\n"
    "    'node: stub-node\\nstatus: complete\\ncommits: []\\n'\n"
    ")\n"
    "time.sleep(300)\n"
)

DECLARED_MUTATION = (
    "reading the stale complete phase again kills the repair in the new test"
)

NEGATIVE_CONTROL_ENV = "RECKON_RESUMED_PHASE_NEGATIVE_CONTROL"
NEGATIVE_CONTROL = os.environ.get(NEGATIVE_CONTROL_ENV, "").strip()

GRACE_SECONDS = 2
QUIET_SECONDS = 0.5
POLICY_SLACK_SECONDS = 8

RUN_ID = "r-resumed-repair"


def _control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation: a delivered word needs no receipt."""
    if NEGATIVE_CONTROL in {"phase-authoritative", "1"}:
        monkeypatch.setattr(
            dispatch_launch_module, "_attempt_ending_receipt", lambda *_a: True
        )
        monkeypatch.setattr(
            dispatch_launch_module, "_reopen_a_live_attempt_phase", lambda *_a: None
        )


def _running(pid: int | None) -> bool:
    """Whether a pid is a live (non-zombie) process."""
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def _worker_pid(run_id: str) -> int | None:
    """The pid the attempt's own worker receipt names, if the worker started."""
    path = runs.run_dir(run_id) / dispatch_launch_module.WORKER_RECORD_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = payload.get("pid") if isinstance(payload, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _wait_until(predicate: Callable[[], Any], *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{detail} not met within {timeout:g}s")


def _kill(pid: int | None) -> None:
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _drive(spec_path: Path, driver: Callable[[], None]) -> None:
    """Run the supervisor in the main thread while ``driver`` observes."""
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    thread = threading.Thread(target=driver, name="resumed-repair-driver")
    thread.start()
    try:
        dispatch_launch_module._run_supervisor(spec_path)
    finally:
        thread.join(timeout=90)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _restore_the_delivered_word(run_id: str, *, attempt_started_at: str) -> None:
    """Write ``complete`` onto a phase its live attempt has not delivered.

    This is what the phase's other writers do: they put back what they read
    before the run was resumed. The pointer also names the attempt its
    supervisor is running, which is what the resumed run's own record carries:
    the pointer and the spec agree on the attempt's start in the measured record
    (both at ``2026-11-15T18:28:29Z``). Written under the pointer's own lock.
    """

    def restore(record: dict[str, Any]) -> dict[str, Any]:
        record["phase"] = "complete"
        record["attempt_started_at"] = attempt_started_at
        return record

    runs._mutate_pointer(run_id, restore)


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stub: str = STUB_WORKING,
) -> dict[str, Any]:
    """A run that delivered attempt 1, and the seam that resumes it as attempt 2."""
    config_hook = tmp_path / "config"
    (config_hook / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
    monkeypatch.setenv(
        dispatch_launch_module.TERMINAL_MANIFEST_GRACE_ENV, str(GRACE_SECONDS)
    )
    # The clock a stored terminal phase arms runs on the module's own default
    # rather than the environment's grace, so both are shortened here: a case
    # that never arms it can then be told apart from one that does.
    monkeypatch.setattr(
        dispatch_launch_module, "TERMINAL_MANIFEST_GRACE_DEFAULT", float(GRACE_SECONDS)
    )
    monkeypatch.setattr(
        dispatch_launch_module, "_WORKER_MANIFEST_QUIET_SECONDS", QUIET_SECONDS
    )
    monkeypatch.setattr(dispatch_launch_module, "_WORKER_MANIFEST_POLL_SECONDS", 0.2)
    monkeypatch.setattr(dispatch_launch_module, "_require_fleet_gate_open", dict)
    monkeypatch.setattr(resumption_module, "_require_fleet_gate_open", dict)
    _control(monkeypatch)

    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: stub-node\nstatus: complete\ncommits: []\n", encoding="utf-8"
    )
    stream = directory / "stream.jsonl"
    prompt = directory / "prompt.txt"
    prompt.write_text("stub prompt\n", encoding="utf-8")

    # Attempt 1 delivered, and the receipts that say so are on disk: the
    # attempt's own exit receipt and the canonical one a launch clears.
    delivered: dict[str, Any] = {
        "run_id": RUN_ID,
        "attempt": 1,
        "ended_during": "working",
        "exited_at": "2026-11-15T10:00:00Z",
        "signal": None,
        "exit_code": 0,
        "worker_pid": 424242,
    }
    runs._write_json(directory / dispatch_launch_module.EXIT_RECORD_NAME, delivered)
    runs._write_json(directory / "attempt-1-exit.json", delivered)

    pointer: dict[str, Any] = {
        "run_id": RUN_ID,
        "project": "",
        "repo": str(worktree),
        "worktree": str(worktree),
        "backend": "alpha",
        "launch": "cli",
        "dialect": "claude",
        "phase": "complete",
        "pid": None,
        "session": "coordinator-fixture",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-11-15T10:00:00Z",
        "created_at": "2026-11-15T09:00:00Z",
        "node": {
            "id": "stub-node",
            "plan": "fixture",
            "time_budget": "20m",
            "manifest_path": str(manifest),
        },
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)

    plan = _backends.LaunchPlan(
        backend="alpha",
        dialect="claude",
        argv=[sys.executable, "-c", stub],
        cwd=str(worktree),
        stdin_text="",
        environment={"RECKON_MANIFEST": str(manifest)},
        final_message_path=None,
        resumed_session=None,
    )
    monkeypatch.setattr(resumption_module, "resume_plan", lambda *_a, **_k: plan)

    captured: dict[str, Path] = {}

    def _capture(spec_path: Path, _run_directory: Path, _run_id: str) -> int:
        captured["spec_path"] = Path(spec_path)
        return 424243

    monkeypatch.setattr(dispatch_launch_module, "_start_supervisor", _capture)
    return {
        "run_id": RUN_ID,
        "directory": directory,
        "manifest": manifest,
        "captured": captured,
    }


def _resume_through_the_hand_door(run_id: str) -> None:
    """Resume as an operator does, through ``crew resume``."""
    result = CliRunner().invoke(crew_resume, ["--run", run_id, "--advice", "continue"])
    assert result.exit_code == 0, result.output


def _resume_through_the_launcher(run_id: str) -> None:
    """Resume as the folded finding and the reflex sweep do, through ``_resume``."""
    record = runs.read_pointer(run_id)
    resumption_module._resume(run_id, record, config=None, advice="continue")


def _not_killed_case(
    fixture: dict[str, Any], *, prelaunch: Callable[[str], None]
) -> None:
    """Resume, restore the stale word, drive the supervisor, and prove no signal."""
    run_id = fixture["run_id"]
    prelaunch(run_id)
    spec_path = fixture["captured"]["spec_path"]

    # The resume's own record: the delivery it replaces was kept beside the
    # manifest and the pointer names the attempt it started from.
    assert (
        fixture["manifest"]
        .read_text(encoding="utf-8")
        .startswith("node: stub-node\nstatus: in-progress\n")
    ), "the delivered status word was not reset before the new attempt spawned"
    resumed = runs.read_pointer(run_id)
    assert resumed["attempt"] == 2, resumed
    assert resumed["attempt_kind"] == "resume", resumed
    assert resumed["phase"] == "working", resumed

    failures: list[BaseException] = []
    observed: list[str] = []

    def driver() -> None:
        pid: int | None = None
        try:
            _wait_until(
                lambda: _worker_pid(run_id) is not None,
                timeout=15,
                detail="the stub worker's record",
            )
            pid = _worker_pid(run_id)
            # The writers that restore the phase they read before the resume.
            # At base this word arms the guard's terminal clock on a live
            # attempt that has delivered nothing.
            _restore_the_delivered_word(
                run_id,
                attempt_started_at=str(
                    json.loads(spec_path.read_text(encoding="utf-8")).get(
                        "attempt_started_at"
                    )
                    or ""
                ),
            )
            time.sleep(QUIET_SECONDS + GRACE_SECONDS + POLICY_SLACK_SECONDS)
            if _running(pid):
                observed.append("kept")
            else:
                failures.append(
                    AssertionError(
                        "a resumed repair was signalled on the delivered word its "
                        "earlier attempt left; the live attempt had no exit receipt"
                    )
                )
            # The run reads as working again rather than as a finished attempt
            # with a live process.
            phase = str(runs.read_pointer(run_id).get("phase") or "")
            if phase != "working":
                failures.append(
                    AssertionError(f"the live repair reads as {phase!r}, not working")
                )
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)
        finally:
            _kill(pid)

    _drive(spec_path, driver)
    assert failures == [], failures
    assert observed == ["kept"]

    # The attempt's own record carries no forced exit: it was not ended as a
    # terminal run, and the reason a reader would find there names the kill the
    # driver's cleanup performed, if any.
    receipt = json.loads((fixture["directory"] / "attempt-2-exit.json").read_text())
    assert not receipt.get("forced_exit"), receipt


def test_a_resumed_repair_is_not_signalled_through_the_hand_door(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _not_killed_case(
        _fixture(tmp_path, monkeypatch), prelaunch=_resume_through_the_hand_door
    )


def test_a_resumed_repair_is_not_signalled_through_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _not_killed_case(
        _fixture(tmp_path, monkeypatch), prelaunch=_resume_through_the_launcher
    )


def test_a_repair_that_delivers_is_still_ended_within_its_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt that really did deliver is ended as before."""
    fixture = _fixture(tmp_path, monkeypatch, stub=STUB_DELIVERS)
    run_id = fixture["run_id"]
    _resume_through_the_launcher(run_id)
    spec_path = fixture["captured"]["spec_path"]

    failures: list[BaseException] = []
    observed: list[str] = []

    def driver() -> None:
        pid: int | None = None
        try:
            _wait_until(
                lambda: _worker_pid(run_id) is not None,
                timeout=15,
                detail="the stub worker's record",
            )
            pid = _worker_pid(run_id)
            deadline = (
                time.time() + QUIET_SECONDS + GRACE_SECONDS + POLICY_SLACK_SECONDS
            )
            while time.time() < deadline:
                if not _running(pid):
                    observed.append("ended")
                    return
                time.sleep(0.05)
            failures.append(
                AssertionError(
                    "a resumed attempt that delivered its own verdict was not ended "
                    "inside the grace"
                )
            )
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            _kill(pid)

    _drive(spec_path, driver)
    assert failures == [], failures
    assert observed == ["ended"]

    receipt = json.loads((fixture["directory"] / "attempt-2-exit.json").read_text())
    assert receipt.get("attempt") == 2, receipt
    assert receipt.get("forced_exit"), (
        "a delivered attempt's own exit receipt must record the ending"
    )


def test_a_delivered_word_is_read_only_with_its_attempts_own_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's reading, both directions, without the driver's timing."""
    fixture = _fixture(tmp_path, monkeypatch)
    directory = fixture["directory"]
    attempt = {"attempt": 2}

    # Attempt 1 delivered and has its receipt; attempt 2 is live and has none.
    assert (
        dispatch_launch_module._terminal_attempt_phase(directory, {"attempt": 1})
        == "complete"
    ), "an attempt with its own exit receipt must still read as terminal"

    runs._mutate_pointer(
        RUN_ID, lambda record: {**record, "attempt": 2, "phase": "complete"}
    )
    assert dispatch_launch_module._terminal_attempt_phase(directory, attempt) == "", (
        "a live attempt must not inherit the delivered word of the one before it"
    )

    # A deliberate stop is not a delivery word and is left as the run's fact.
    runs._mutate_pointer(RUN_ID, lambda record: {**record, "phase": "stopped"})
    assert (
        dispatch_launch_module._terminal_attempt_phase(directory, attempt) == "stopped"
    ), "a stopped attempt still reads as terminal without an exit receipt"


def test_a_receipt_for_another_run_does_not_end_this_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file standing where this attempt's receipt goes is judged on its run.

    The two exit files are read through the one owner of that payload under its
    strict reading, so a receipt that names a different run is absent rather
    than this attempt's ending — the failure a hand-rolled reader, which judged only
    the attempt number, could not see.
    """
    fixture = _fixture(tmp_path, monkeypatch)
    directory = fixture["directory"]
    attempt = {"attempt": 2}
    runs._mutate_pointer(
        RUN_ID, lambda record: {**record, "attempt": 2, "phase": "complete"}
    )
    receipt_path = directory / "attempt-2-exit.json"
    receipt_path.write_text(
        json.dumps({"attempt": 2, "run_id": "r-someone-else"}), encoding="utf-8"
    )
    assert dispatch_launch_module._terminal_attempt_phase(directory, attempt) == "", (
        "a receipt for another run must not end this attempt"
    )

    receipt_path.write_text(
        json.dumps({"attempt": 2, "run_id": RUN_ID}), encoding="utf-8"
    )
    assert (
        dispatch_launch_module._terminal_attempt_phase(directory, attempt) == "complete"
    ), "the same receipt naming this run does end it"
