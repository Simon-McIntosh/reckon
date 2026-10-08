"""crew recover classifies a queued pointer that names no log path.

A held local dispatch is stored durably before anything launches, so its
pointer carries ``phase: queued`` and no ``log_path``. Recovery re-observes
every live pointer, and a queued pointer has no stream to observe — the fold
keeps its stored fields and the classifier reads it ``queued``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _config() -> dict:
    """A flight config naming the lane a queued dispatch was held on."""
    return {
        "backends": {
            "clive": {
                "dialect": "claude",
                "command": "claude",
                "usable_input_window": 200000,
            }
        }
    }


def _queued_request(run_id: str) -> dict:
    """The request a held local dispatch stores, as queue_dispatch writes it."""
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": "proj",
        "session": "session",
        "node": {"id": "held-node", "plan": "plan-a", "section": "dispatch"},
        "backend": "clive",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "reason": "waiting for a local lane slot",
    }


def _write_launched_pointer(crew_home: Path, run_id: str) -> dict:
    """One launched cli run whose pointer names a stream that exists."""
    stream = crew_home / "crew" / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": "launched-node", "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "launch": "cli",
        "backend": "clive",
        "log_path": str(stream),
        "manifest_path": str(crew_home / "crew" / "manifests" / f"{run_id}.md"),
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def test_recover_reads_a_queued_run(crew_home: Path) -> None:
    queued, position = runs.queue_dispatch(_queued_request("r-queued"))
    assert queued["phase"] == "queued"
    assert "log_path" not in queued
    assert position == 1

    _write_launched_pointer(crew_home, "r-launched")

    report = recovery.recover(project="proj", config=_config())

    rows = {row["run_id"]: row for row in report["runs"]}
    assert set(rows) == {"r-queued", "r-launched"}
    assert rows["r-queued"]["classification"] == "queued"

    # The fold kept the queued pointer's stored fields rather than observing a
    # stream the queued run does not have.
    stored = runs.read_pointer("r-queued")
    assert stored["phase"] == "queued"
    assert stored["reason"] == "waiting for a local lane slot"
