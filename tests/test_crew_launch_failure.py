"""A worker that exits before its first stream record is one launch failure.

The fault: a watcher armed without the backend directory launched a backend
that died at exec, leaving a 0-byte stream every two minutes while the pointer
kept reading working. Twelve such files accumulated on one run. The exit was
known to the reaper and read against nothing.

The backend here is a synthetic script that exits 127 immediately, launched
through the real spawn and reaped on a real tick, so the record, the phase and
the stopped lift are all exercised end to end rather than asserted about a
hand-built pointer.
"""

from __future__ import annotations

import importlib
import signal
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import resumption, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RUN_ID = "r-launch-failure"
# A launch that dies at exec is the measured shape; 127 is what a shell reports
# for a command it cannot run.
EXIT_STATUS = 127


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _pointer(run_id: str, directory: Path) -> dict:
    return {
        "run_id": run_id,
        "project": "sample",
        "phase": "working",
        "backend": "alpha",
        "launch": "cli",
        "pid": None,
        "worktree": str(directory),
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "node": {"id": "node-launch", "plan": "fixture"},
    }


def _synthetic_backend(path: Path) -> Path:
    """A backend that dies before writing anything, like a failed exec."""
    path.write_text(
        f"#!/bin/sh\necho 'exec: cannot run the backend' >&2\nexit {EXIT_STATUS}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _launch_through_the_reaper(
    tmp_path: Path, run_id: str = RUN_ID
) -> tuple[Path, dict]:
    """Spawn one doomed worker the way a dispatch does, and reap it."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    runs._write_json(runs.pointer_path(run_id), _pointer(run_id, directory))

    backend = _synthetic_backend(tmp_path / "doomed-backend")
    plan = dispatch_module.resolve_launch_executable(
        importlib.import_module("reckon._backends").LaunchPlan(
            backend="alpha",
            dialect="claude",
            argv=[str(backend)],
            cwd=str(directory),
            stdin_text="",
            environment={},
            final_message_path=None,
            resumed_session=None,
        )
    )
    prompt = directory / "prompt.txt"
    prompt.write_text("do the work\n", encoding="utf-8")

    dispatch_module._spawn(
        plan,
        log_path=directory / "stream.jsonl",
        stderr_path=directory / "stderr.log",
        prompt_path=prompt,
    )
    return directory, plan


def _wait_for_phase(run_id: str, phase: str, timeout: float = 15.0) -> dict:
    """Poll the pointer until the reaper records the phase, or fail loudly."""
    deadline = time.monotonic() + timeout
    record = runs.read_pointer(run_id)
    while time.monotonic() < deadline:
        record = runs.read_pointer(run_id)
        if str(record.get("phase") or "") == phase:
            return record
        dispatch_module._reap_launched_workers()
        time.sleep(0.05)
    raise AssertionError(
        f"run {run_id!r} stayed in phase {record.get('phase')!r}; "
        f"launch_failures={record.get('launch_failures')!r}"
    )


def test_a_launch_that_wrote_a_turn_is_not_a_launch_failure(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The control: an exit is only a launch failure when the stream is empty.

    Without this, a record on every early exit would pass the case above for
    the wrong reason — the guard is the emptiness of the stream, not the exit.
    """
    run_id = "r-launch-with-output"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    runs._write_json(runs.pointer_path(run_id), _pointer(run_id, directory))

    backend = tmp_path / "chatty-backend"
    backend.write_text(
        f'#!/bin/sh\necho \'{{"type": "assistant"}}\'\nexit {EXIT_STATUS}\n',
        encoding="utf-8",
    )
    backend.chmod(0o755)
    plan = dispatch_module.resolve_launch_executable(
        importlib.import_module("reckon._backends").LaunchPlan(
            backend="alpha",
            dialect="claude",
            argv=[str(backend)],
            cwd=str(directory),
            stdin_text="",
            environment={},
            final_message_path=None,
            resumed_session=None,
        )
    )
    prompt = directory / "prompt.txt"
    prompt.write_text("do the work\n", encoding="utf-8")
    dispatch_module._spawn(
        plan,
        log_path=directory / "stream.jsonl",
        stderr_path=directory / "stderr.log",
        prompt_path=prompt,
    )

    # Reap until the reaper has actually consumed this child. Asserting only on
    # the absence of a record would pass for a process that was never reaped,
    # which is the vacuity this control exists to rule out.
    stream = directory / "stream.jsonl"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        dispatch_module._reap_launched_workers()
        with dispatch_module._LAUNCHED_WORKERS_LOCK:
            outstanding = list(dispatch_module._LAUNCHED_WORKER_RUNS.values())
        if not any(entry.get("stream_path") == str(stream) for entry in outstanding):
            break
        time.sleep(0.05)

    with dispatch_module._LAUNCHED_WORKERS_LOCK:
        outstanding = list(dispatch_module._LAUNCHED_WORKER_RUNS.values())
    assert not any(entry.get("stream_path") == str(stream) for entry in outstanding)
    assert stream.stat().st_size > 0

    record = runs.read_pointer(run_id)
    assert record.get("launch_failures") in (None, [])
    assert record["phase"] == "working"


def _wait_for_wait_status(run_id: str, timeout: float = 15.0) -> dict:
    """Poll until the reaper has written the wait status, or fail loudly.

    The assertion is on the record, not on the stream: what a reader acts on is
    what the launcher kept, so the test reads the same surface.
    """
    deadline = time.monotonic() + timeout
    record = runs.read_pointer(run_id)
    while time.monotonic() < deadline:
        record = runs.read_pointer(run_id)
        if record.get("wait_status"):
            return record
        dispatch_module._reap_launched_workers()
        time.sleep(0.05)
    raise AssertionError(
        f"run {run_id!r} never recorded a wait status; record={record!r}"
    )


def _spawn_a_stub_backend(tmp_path: Path, run_id: str, body: str) -> Path:
    """Spawn a stub backend whose last act is ``body``, and reap it ourselves.

    The child is consumed with the same tight poll the control above uses, so a
    test asserting the absence of a launch failure cannot pass because nothing
    was reaped.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    runs._write_json(runs.pointer_path(run_id), _pointer(run_id, directory))
    backend = tmp_path / "stub-backend"
    backend.write_text(body, encoding="utf-8")
    plan = dispatch_module.resolve_launch_executable(
        importlib.import_module("reckon._backends").LaunchPlan(
            backend="alpha",
            dialect="claude",
            argv=["/bin/sh", str(backend)],
            cwd=str(directory),
            stdin_text="",
            environment={},
            final_message_path=None,
            resumed_session=None,
        )
    )
    prompt = directory / "prompt.txt"
    prompt.write_text("do the work\n", encoding="utf-8")
    dispatch_module._spawn(
        plan,
        log_path=directory / "stream.jsonl",
        stderr_path=directory / "stderr.log",
        prompt_path=prompt,
    )
    stream = directory / "stream.jsonl"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        dispatch_module._reap_launched_workers()
        with dispatch_module._LAUNCHED_WORKERS_LOCK:
            outstanding = list(dispatch_module._LAUNCHED_WORKER_RUNS.values())
        if not any(entry.get("stream_path") == str(stream) for entry in outstanding):
            break
        time.sleep(0.05)
    return directory


def test_a_worker_that_ends_by_signal_records_that_signal_on_its_run(
    isolated_home: Path, tmp_path: Path
) -> None:
    """A worker killed by a signal is legible on its own run record.

    The stream is non-empty, so the record can only have come from the wait the
    launcher held: discarding the status for a run that wrote a turn is what
    leaves a killed worker reading as a live one. The phase stays ``working``
    because this node records the death and does not yet classify it.
    """
    run_id = "r-signalled-worker"
    body = '#!/bin/sh\necho \'{"type": "assistant"}\'\nkill -TERM $$\n'
    directory = _spawn_a_stub_backend(tmp_path, run_id, body)

    record = _wait_for_wait_status(run_id)
    wait = record["wait_status"]
    assert wait["signal"] == signal.SIGTERM
    assert wait["signal_name"] == "SIGTERM"
    assert wait["exit_code"] is None
    # Non-empty: this run wrote a turn, so it is not a launch failure and the
    # status is the only place the death appears.
    assert (directory / "stream.jsonl").stat().st_size > 0
    assert record["phase"] == "working"
    assert list(record.get("launch_failures") or ()) == []


def test_a_worker_that_exits_records_its_code_and_no_signal(
    isolated_home: Path, tmp_path: Path
) -> None:
    """An orderly exit records the code it chose, and names no signal."""
    run_id = "r-exiting-worker"
    body = '#!/bin/sh\necho \'{"type": "assistant"}\'\nexit 3\n'
    directory = _spawn_a_stub_backend(tmp_path, run_id, body)

    record = _wait_for_wait_status(run_id)
    wait = record["wait_status"]
    assert wait["exit_code"] == 3
    assert wait["signal"] is None
    assert wait["signal_name"] is None
    assert (directory / "stream.jsonl").stat().st_size > 0
    assert list(record.get("launch_failures") or ()) == []


def test_an_empty_stream_is_recorded_once_and_stops_the_lift(
    isolated_home: Path, tmp_path: Path
) -> None:
    directory, plan = _launch_through_the_reaper(tmp_path)

    record = _wait_for_phase(RUN_ID, dispatch_module.LAUNCH_FAILED_PHASE)

    failures = list(record.get("launch_failures") or ())
    assert len(failures) == 1
    failure = failures[0]
    assert failure["exit_status"] == EXIT_STATUS
    assert failure["argv"] == list(plan.argv)
    assert failure["backend"] == "alpha"
    # The stream is empty because the launch never wrote a turn, which is the
    # fact that makes this a launch failure rather than a worker turn.
    assert (directory / "stream.jsonl").stat().st_size == 0
    assert "cannot run the backend" in failure["stderr_tail"]

    # A second tick reaps nothing (the pid was consumed) and relaunches
    # nothing: the run is off the lift loop until a person acts.
    dispatch_module._reap_launched_workers()
    again = runs.read_pointer(RUN_ID)
    assert again["phase"] == dispatch_module.LAUNCH_FAILED_PHASE
    assert len(list(again.get("launch_failures") or ())) == 1


def test_a_second_recording_for_one_launch_does_not_double_record(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The once-only guard, reached twice without a reap in between.

    The end-to-end cases show one record per launch, but the once-ness they
    measure comes from the pid being consumed by ``os.waitpid`` before a second
    tick can reach the recorder, not from the guard the code relies on. A second
    reaper or a handover edge that reaches the recorder twice for one launch
    would double-record, so the guard is reached directly here: two recordings,
    one run, one failure.
    """
    run_id = "r-double-record"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    runs._write_json(runs.pointer_path(run_id), _pointer(run_id, directory))
    launched = {
        "run_id": run_id,
        "stream_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "argv": ["/bin/false"],
        "backend": "alpha",
    }

    dispatch_module._record_launch_failure(launched, exit_status=EXIT_STATUS)
    dispatch_module._record_launch_failure(launched, exit_status=EXIT_STATUS)

    record = runs.read_pointer(run_id)
    failures = list(record.get("launch_failures") or ())
    assert len(failures) == 1
    assert record["phase"] == dispatch_module.LAUNCH_FAILED_PHASE
    assert failures[0]["exit_status"] == EXIT_STATUS


def test_the_lift_refuses_a_launch_failed_run_but_a_person_may_still_resume(
    isolated_home: Path, tmp_path: Path
) -> None:
    _launch_through_the_reaper(tmp_path)
    record = _wait_for_phase(RUN_ID, dispatch_module.LAUNCH_FAILED_PHASE)

    launched: list[dict] = []

    def launcher(plan, **kwargs):  # pragma: no cover - must never be reached
        launched.append({"plan": plan, **kwargs})
        return 4321

    with pytest.raises(crew.CrewError) as refusal:
        resumption._resume(
            RUN_ID, record, config=None, launcher=launcher, advice="continue"
        )

    message = str(refusal.value)
    assert dispatch_module.LAUNCH_FAILED_PHASE in message
    assert str(EXIT_STATUS) in message
    assert launched == []
    # Nothing the refused lift would have written appeared.
    assert list(runs.run_dir(RUN_ID).glob("resume-*")) == []


def test_a_launch_failed_run_is_counted_as_actionable(
    isolated_home: Path, tmp_path: Path
) -> None:
    """A run wanting a repaired command is work for a person, not inventory.

    The fleet reading counts runs whose classification is actionable. A
    launch-failed run was classified on its own state and then left out of that
    count, so it occupied a lane and read as invisible — the same failure as
    leaving it in the working bucket, one layer over.
    """
    import json

    from reckon.crew import promotion

    (isolated_home / "mounts.json").write_text(
        json.dumps({"sample": str(isolated_home / "docs")}), encoding="utf-8"
    )
    _launch_through_the_reaper(tmp_path)
    _wait_for_phase(RUN_ID, dispatch_module.LAUNCH_FAILED_PHASE)

    reading = promotion._fleet_state_reading("sample")

    assert reading["fleet_state"] == "measured"
    assert reading["actionable_runs"] == 1
    assert reading["actionable_classifications"] == ["launch-failed"]
