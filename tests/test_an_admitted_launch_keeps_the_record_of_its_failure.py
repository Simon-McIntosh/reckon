"""A launch that loses its pointer records what it saw instead of guessing.

An admitted launch releases the queued pointer, publishes its own pointer under
the same run id, and only then starts the supervisor — whose first act is to
read that pointer. When something removes it inside that window the attempt
ends with ``worker did not spawn: CrewError: no live run ...``, the pane offers
a remedy about PATH that names nothing, and the next launch overwrites the only
record of the failure.

So the supervisor records the evidence at the failure — the run directory as it
stood, whether the admitted request is still held, and which pass admitted it —
keeps every attempt's exit record under its own number, and names the missing
pointer in the record's own detail. The declared mutation removes the
launch-failure record's evidence and the per-attempt numbering; the test then
finds no such record, and the relaunched attempt overwrites the first one's.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import dispatch_launch, runs

RUN_ID = "r-admitted-launch-lost-its-pointer"
EXEC_RUN_ID = "r-admitted-launch-cannot-exec"
ADMITTING_PASS = {
    "pid": 4242,
    "at": "2026-10-29T08:20:00Z",
    "code": "0123456789abcdef",
}
ATTEMPT_STARTED_AT = "2026-10-29T08:20:01Z"


def _pointer(run_id: str, *, phase: str = "starting") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": "admitted-launch-fixture",
        "repo": "",
        "backend": "alpha",
        "launch": "cli",
        "dialect": "claude",
        "phase": phase,
        "pid": None,
        "session": "coordinator-fixture",
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": ATTEMPT_STARTED_AT,
        "created_at": ATTEMPT_STARTED_AT,
        "node": {
            "id": "stub-node",
            "plan": "fixture",
            "section": "s1",
            "manifest_path": "",
        },
    }


def _spec(run_id: str, directory: Path, worktree: Path, *, argv: list[str]) -> Path:
    stream = directory / "stream.jsonl"
    prompt = directory / "prompt.txt"
    prompt.write_text("stub\n", encoding="utf-8")
    spec = {
        "run_id": run_id,
        "run_directory": str(directory),
        "repo": str(worktree),
        "worktree": str(worktree),
        "fenced": False,
        "plan": {
            "argv": argv,
            "cwd": str(worktree),
            "environment": {},
            "dialect": "claude",
            "backend": "alpha",
        },
        "prompt_path": str(prompt),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": ATTEMPT_STARTED_AT,
        "environment": {},
    }
    path = directory / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _admitted_run(tmp_path: Path, run_id: str, *, argv: list[str]) -> dict[str, Any]:
    """A run whose dispatch published its pointer and whose supervisor now runs.

    Everything dispatch does before it starts the supervisor is done here: the
    run directory exists, the attempt records are prepared, and the admission
    pass's handover is written. The pointer is then taken away, which is the
    state the launch failure was measured in.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    runs._write_json(runs.pointer_path(run_id), _pointer(run_id))
    dispatch_launch._prepare_attempt_records(
        directory,
        run_id=run_id,
        attempt=1,
        attempt_kind="dispatch",
        attempt_started_at=ATTEMPT_STARTED_AT,
    )
    pointer = _pointer(run_id, phase="queued")
    runs.write_handover(pointer, admitted_by=ADMITTING_PASS)
    runs.pointer_path(run_id).unlink(missing_ok=True)
    return {
        "run_id": run_id,
        "directory": directory,
        "spec_path": _spec(run_id, directory, worktree, argv=argv),
    }


def _supervise(spec_path: Path) -> int:
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return dispatch_launch._run_supervisor(spec_path)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _exit_record(directory: Path, attempt: int) -> dict[str, Any]:
    path = directory / f"attempt-{attempt}-{dispatch_launch.EXIT_RECORD_NAME}"
    assert path.exists(), f"no exit record for attempt {attempt} at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_missing_pointer_is_recorded_with_what_stood_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor writes what it saw, and names the missing pointer."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    fixture = _admitted_run(tmp_path, RUN_ID, argv=["/bin/true"])
    directory = fixture["directory"]

    assert _supervise(fixture["spec_path"]) == 0

    record = _exit_record(directory, 1)
    failure = record["launch_failure"]
    assert failure["kind"] == "pointer-missing", (
        f"a launch that found no pointer was not recorded as one: {failure!r}"
    )
    assert failure["exception_type"] == "CrewError", (
        f"the record replaced the exception's own type: {failure['exception_type']!r}"
    )
    assert "no live run" in failure["message"], (
        f"the record lost read_pointer's message: {failure['message']!r}"
    )

    listing = failure["run_directory_listing"]
    assert "attempt.json" in listing and "spec.json" in listing, (
        f"the record does not carry the run directory it saw: {listing!r}"
    )
    assert dispatch_launch.EXIT_RECORD_NAME not in listing, (
        "the listing was taken after this record was written, so it cannot be "
        f"what the supervisor saw: {listing!r}"
    )
    assert failure["pointer_present"] is False
    assert failure["queued_pointer_present"] is True, (
        "the admitted request was still held in a handover, and the record "
        f"does not say so: {failure!r}"
    )
    assert failure["admitting_pass"] == ADMITTING_PASS, (
        f"the record does not name the pass that admitted the launch: {failure!r}"
    )

    detail = record["detail"]
    assert str(runs.pointer_path(RUN_ID)) in detail, (
        f"the detail does not name the missing pointer: {detail!r}"
    )
    assert "PATH=" not in detail, (
        "a missing pointer was reported as a command and PATH problem, which "
        f"points the reader at the wrong thing: {detail!r}"
    )


def test_an_executable_that_cannot_be_run_still_names_the_command_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the branch keeps the remedy it had."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    fixture = _admitted_run(
        tmp_path, EXEC_RUN_ID, argv=[str(tmp_path / "no-such-backend")]
    )
    directory = fixture["directory"]
    # The pointer is present, so the read succeeds and the spawn is what fails.
    runs._write_json(runs.pointer_path(EXEC_RUN_ID), _pointer(EXEC_RUN_ID))

    assert _supervise(fixture["spec_path"]) == 0

    record = _exit_record(directory, 1)
    assert record["launch_failure"]["kind"] == "spawn", (
        f"a command that could not be run was recorded as something else: "
        f"{record['launch_failure']!r}"
    )
    detail = record["detail"]
    assert "no-such-backend" in detail, (
        f"the detail does not name the command that could not be run: {detail!r}"
    )
    assert "PATH=" in detail, (
        f"the detail lost the PATH the command was looked for on: {detail!r}"
    )


def test_a_relaunch_keeps_the_earlier_attempts_exit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every attempt keeps its own exit record under its own number."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    fixture = _admitted_run(tmp_path, RUN_ID, argv=["/bin/true"])
    directory = fixture["directory"]

    assert _supervise(fixture["spec_path"]) == 0
    first = _exit_record(directory, 1)

    # The admission pass puts the request back and launches it again under the
    # same run id, so the relaunch arrives with the attempt number its
    # predecessor had.
    runs.write_handover(_pointer(RUN_ID, phase="queued"), admitted_by=ADMITTING_PASS)
    dispatch_launch._prepare_attempt_records(
        directory,
        run_id=RUN_ID,
        attempt=1,
        attempt_kind="dispatch",
        attempt_started_at=ATTEMPT_STARTED_AT,
    )
    assert _supervise(fixture["spec_path"]) == 0

    assert _exit_record(directory, 1) == first, (
        "the relaunch overwrote the first attempt's exit record, which is the "
        "only account of how that attempt ended"
    )
    second = _exit_record(directory, 2)
    assert second["attempt"] == 2
    assert second["launch_failure"]["kind"] == "pointer-missing"
