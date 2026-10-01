"""The supervisor advances a live pointer's stored phase past ``starting``.

The launcher writes ``starting`` and, without a writer, nothing moves it: the
follower's re-arm replay, the MCP views and a peer's tooling all read the
pre-spawn label for the whole life of the run. The supervisor is the only
process that touches the pointer while the run is alive, so it writes the
advance — ``working`` once the worker is spawned, and the delivered manifest's
phase once the worker has exited.

A stub run is driven through that whole life: a pointer at ``starting``, a
worker that writes its first stream record, and a terminal manifest. The
declared mutation replaces the supervisor's writer with a no-op, and the stored
phase then stays ``starting`` whatever the run does.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The stub worker writes its first stream record, then holds until the test
# releases it, then writes a terminal manifest and exits -- so the three phases
# are observed in order rather than raced.
STUB_WORKER = (
    "import json, os, time\n"
    "from pathlib import Path\n"
    "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
    "trigger = Path(os.environ['RECKON_TRIGGER'])\n"
    "while not trigger.exists():\n"
    "    time.sleep(0.01)\n"
    "Path(os.environ['RECKON_MANIFEST']).write_text("
    "'node: stub-node\\nstatus: complete\\ncommits: []\\n')\n"
)

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "remove the supervisor's phase write; the stored phase stays starting and "
    "the test fails"
)

NEGATIVE_CONTROL = os.environ.get("RECKON_STORED_PHASE_NEGATIVE_CONTROL", "").strip()


def _control(monkeypatch: pytest.MonkeyPatch, guard: str):
    """Drop the writer, for the run whose only purpose is to fail without it."""
    if NEGATIVE_CONTROL in {guard, "all"}:
        monkeypatch.setattr(
            dispatch_module, "_publish_stored_phase", lambda *args, **kwargs: None
        )


def _read_phase(run_id: str | None) -> str | None:
    try:
        return str(runs.read_pointer(str(run_id)).get("phase") or "")
    except Exception:  # noqa: BLE001 - a write in flight is simply not the phase
        return None


def _wait_for_phase(run_id: str, phase: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_phase(run_id) == phase:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"stored phase never read {phase!r} within {timeout:g}s "
        f"(last {_read_phase(run_id)!r})"
    )


def _wait_for_stream_record(stream: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if stream.read_text(encoding="utf-8").strip():
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError(f"no stream record in {stream} within {timeout:g}s")


def _stub_run(tmp_path: Path) -> dict[str, Any]:
    """A pointer at ``starting`` and the spec that supervises its stub worker."""
    run_id = "r-stored-phase"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = tmp_path / "manifest.md"
    trigger = tmp_path / "release"
    stream = directory / "stream.jsonl"
    prompt = directory / "prompt.txt"
    prompt.write_text("stub\n", encoding="utf-8")

    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "stored-phase-fixture",
            "repo": str(worktree),
            "worktree": str(worktree),
            "backend": "alpha",
            "launch": "cli",
            "dialect": "claude",
            "phase": "starting",
            "pid": None,
            "session": "coordinator-fixture",
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "stderr_path": str(directory / "stderr.log"),
            "attempt": 1,
            "attempt_kind": "dispatch",
            "attempt_started_at": "2026-10-01T12:00:00Z",
            "created_at": "2026-10-01T12:00:00Z",
            "node": {
                "id": "stub-node",
                "plan": "fixture",
                "time_budget": "20m",
                "manifest_path": str(manifest),
            },
        },
    )

    spec = {
        "run_id": run_id,
        "run_directory": str(directory),
        "repo": str(worktree),
        "worktree": str(worktree),
        "fenced": False,
        "plan": {
            "argv": [sys.executable, "-c", STUB_WORKER],
            "cwd": str(worktree),
            "environment": {"RECKON_TRIGGER": str(trigger)},
            "dialect": "claude",
            "backend": "alpha",
        },
        "prompt_path": str(prompt),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": "2026-10-01T12:00:00Z",
        "environment": {},
    }
    spec_path = directory / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return {
        "run_id": run_id,
        "manifest": manifest,
        "trigger": trigger,
        "stream": stream,
        "spec_path": spec_path,
    }


def test_stored_phase_advances_from_starting_to_working_to_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    fixture = _stub_run(tmp_path)
    run_id = fixture["run_id"]
    trigger = fixture["trigger"]
    stream = fixture["stream"]
    _control(monkeypatch, "phase-write")

    assert _read_phase(run_id) == "starting"

    observed: list[str] = []
    failures: list[BaseException] = []

    def driver() -> None:
        try:
            _wait_for_phase(run_id, "working", timeout=15)
            _wait_for_stream_record(stream, timeout=15)
            # The worker is demonstrably running, yet the phase holds at working.
            assert _read_phase(run_id) == "working"
            observed.append("working")
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)
        finally:
            trigger.write_text("go", encoding="utf-8")
        try:
            _wait_for_phase(run_id, "complete", timeout=15)
            observed.append("complete")
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            failures.append(exc)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    sampler = threading.Thread(target=driver, name="phase-sampler")
    sampler.start()
    try:
        dispatch_module._run_supervisor(fixture["spec_path"])
    finally:
        sampler.join(timeout=40)
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    assert failures == [], failures
    assert observed == ["working", "complete"]
    assert fixture["manifest"].is_file()
    assert _read_phase(run_id) == "complete"
