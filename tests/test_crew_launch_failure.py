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
