"""A late observation never reopens a run the supervisor has finished.

The supervisor is the one writer of a terminal stored phase: it sets the
delivered manifest's phase on the live pointer once the worker exits. An
observation folds the run's own stream back into that pointer, and a stream
that was rewritten, truncated, or read before its terminal event reports a
live phase for a run that has already ended. Folding that non-terminal phase
over the stored terminal phase would show a finished run as still running.

The declared mutation drops the guard in ``observe`` so the non-terminal
stream phase overwrites the terminal stored phase, and the first case fails.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# A stream with one event and no terminal event: the observer reports it as
# live, which is what a rewritten or partially-read finished stream looks like.
LIVE_STREAM_LINE = json.dumps(
    {
        "type": "system",
        "subtype": "init",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "claude_code_version": "2.1.246",
    }
)

# A stream that reports a live phase, since it carries no terminal event.
NON_TERMINAL_STREAM = LIVE_STREAM_LINE + "\n"

# The declared mutation, verbatim: the string the promotion audit matches the
# red log's first line against.
DECLARED_MUTATION = (
    "remove the terminal-phase guard from observe(); the terminal phase is "
    "overwritten and the test fails"
)

NEGATIVE_CONTROL = os.environ.get("RECKON_TERMINAL_PHASE_NEGATIVE_CONTROL", "").strip()

CONFIG = {
    "backends": {
        "alpha": {"launch": "cli", "command": "claude", "model": "m", "effort": "high"}
    }
}


def _drop_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the guard, for the run whose only purpose is to fail without it."""
    if NEGATIVE_CONTROL == "terminal-phase-guard":
        monkeypatch.setattr(
            dispatch_module, "_terminal_phase_survives", lambda *args, **kwargs: False
        )


def _pointer(run_id: str, tmp_path: Path, *, phase: str) -> dict[str, Any]:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_text(NON_TERMINAL_STREAM, encoding="utf-8")
    manifest = tmp_path / f"{run_id}-manifest.md"
    pointer = {
        "run_id": run_id,
        "project": "terminal-phase-fixture",
        "repo": str(tmp_path),
        "worktree": str(tmp_path),
        "backend": "alpha",
        "command": "claude",
        "launch": "cli",
        "dialect": "claude",
        "phase": phase,
        "pid": None,
        "process_alive": None,
        "session": "coordinator-fixture",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "attempt": 1,
        "attempt_kind": "dispatch",
        "created_at": "2026-10-01T12:00:00Z",
        "node": {"id": run_id, "plan": "fixture", "manifest_path": str(manifest)},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _stored_phase(run_id: str) -> str:
    return str(json.loads(runs.pointer_path(run_id).read_text()).get("phase") or "")


def test_a_late_observe_keeps_the_terminal_stored_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    _drop_guard(monkeypatch)

    run_id = "r-terminal-stored"
    pointer = _pointer(run_id, tmp_path, phase="starting")
    # The supervisor writes the delivered manifest's terminal phase, exactly as
    # it does once the worker has exited.
    Path(pointer["manifest_path"]).write_text(
        "node: r-terminal-stored\nstatus: complete\ncommits: []\n", encoding="utf-8"
    )
    dispatch_module._publish_stored_phase(
        {"run_id": run_id, "attempt": 1}, ended=True, exit_record=None
    )
    assert _stored_phase(run_id) == "complete"

    observed = crew.observe(run_id, config=CONFIG)

    assert observed["phase"] == "complete"
    assert _stored_phase(run_id) == "complete"


def test_observe_still_advances_a_non_terminal_stored_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    run_id = "r-non-terminal-stored"
    _pointer(run_id, tmp_path, phase="starting")

    observed = crew.observe(run_id, config=CONFIG)

    assert observed["phase"] == "working"
    assert _stored_phase(run_id) == "working"
