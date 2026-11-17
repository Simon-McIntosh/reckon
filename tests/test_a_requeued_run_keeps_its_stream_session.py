"""A run whose record lost its launch command still yields its stream session.

A launch the scheduler refuses re-queues its run, and the re-queued pointer
carries no argv and no command: the composed launch it held belonged to the
attempt that never started. The session the run was continuing is still in the
streams its earlier attempts wrote, and the stream reader needs only a dialect
to read them. Without a recorded command that dialect comes from the run's
configured lane, so the lookup reads the streams through the project's resolved
configuration rather than answering an absence that strands committed work: a
run whose session cannot be found cannot be resumed, only discarded.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from reckon import crew, flight
from reckon.crew import resumption

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)
FIXTURE_SESSION = "01a0635f-62a3-7283-a81b-61cd39bedb60"


def _write(path: Path, *, text: str, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _requeued(run_id: str) -> dict:
    # The pointer a refused launch leaves: queued, no argv, no command, no
    # recorded stream path, no session.
    return {
        "run_id": run_id,
        "project": "proj",
        "phase": "queued",
        "backend": "alpha",
        "launch": "cli",
        "log_path": None,
        "session_id": None,
    }


def _configured(monkeypatch, backends: dict) -> None:
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *_, **__: SimpleNamespace(config={"backends": backends}),
    )


def test_a_requeued_run_resolves_its_session_through_its_lane(
    isolated_reckon_home: Path, monkeypatch
) -> None:
    run_id = "r-requeued"
    directory = crew.run_dir(run_id)
    _write(
        directory / "resume-1.jsonl",
        text=FIXTURE.read_text(encoding="utf-8"),
        mtime=1_000.0,
    )
    # The refused steps' streams: created, never written.
    _write(directory / "resume-2.jsonl", text="", mtime=2_000.0)
    _write(directory / "resume-3.jsonl", text="", mtime=3_000.0)
    _configured(monkeypatch, {"alpha": {"launch": "cli", "command": "codex"}})

    resolved = resumption.resolve_session(run_id, record=_requeued(run_id))

    assert resolved["resolved"] is True
    assert resolved["source"] == "stream"
    assert resolved["session_id"] == FIXTURE_SESSION


def test_a_placed_argv_naming_the_scheduler_reads_through_the_lane(
    isolated_reckon_home: Path, monkeypatch
) -> None:
    # A placed launch's argv opens with the scheduler shim, which names no
    # dialect; the lane's own command does.
    run_id = "r-placed"
    _write(
        crew.run_dir(run_id) / "stream.jsonl",
        text=FIXTURE.read_text(encoding="utf-8"),
        mtime=1_000.0,
    )
    _configured(monkeypatch, {"alpha": {"launch": "cli", "command": "codex"}})
    record = {
        **_requeued(run_id),
        "argv": ["/opt/reckon/host_shims/srun", "--overlap", "--jobid=1", "sh"],
    }

    resolved = resumption.resolve_session(run_id, record=record)

    assert resolved["session_id"] == FIXTURE_SESSION


def test_a_lane_absent_from_the_configuration_is_still_an_absence(
    isolated_reckon_home: Path, monkeypatch
) -> None:
    # The negative arm: with no recorded command and no configured lane there
    # is no dialect to read the stream with, and the lookup says so rather than
    # guessing one.
    run_id = "r-unknown-lane"
    _write(
        crew.run_dir(run_id) / "resume-1.jsonl",
        text=FIXTURE.read_text(encoding="utf-8"),
        mtime=1_000.0,
    )
    _configured(monkeypatch, {})

    resolved = resumption.resolve_session(run_id, record=_requeued(run_id))

    assert resolved["resolved"] is False
    assert resolved["session_id"] is None
