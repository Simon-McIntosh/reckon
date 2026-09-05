"""A resumption does not inherit the superseded attempt's refusal observation.

The refusal classifier reads the pointer's folded budget before it reads the
live stream, and a resume records a brand-new stream. If the previous attempt's
folded budget — carrying the refusal that stopped it — rode into the resumed
attempt, the classifier would short-circuit on that stale observation and keep
reporting the run blocked after the limit had lifted. A resumption must clear
the fold to the honest unknown-headroom state so the reader falls through to
the stream that is actually running.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.crew import recovery

# Recorded provider event streams, read as repository fixtures so a refusal is
# asserted against what a real harness wrote rather than constructed text.
BACKEND_FIXTURES = Path(__file__).parent / "fixtures" / "backends"


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """Move every crew pointer into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _refusal_budget() -> dict:
    """The budget observation the superseded stream folded onto its pointer."""
    observation = _backends.observe_log(
        backend_name="codex",
        backend={"command": "codex"},
        log_path=str(BACKEND_FIXTURES / "codex-usage-limit.jsonl"),
    )
    return observation.as_dict()["budget"]


def _refused_pointer(
    home: Path, run_id: str, *, log_path: Path, manifest: Path, budget: dict
) -> dict:
    """A prior-attempt cli pointer blocked on the refusal the stream recorded."""
    created = datetime(2026, 9, 5, 0, 0, tzinfo=UTC).isoformat()
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "30m"},
        "backend": "codex",
        "launch": "cli",
        "argv": ["codex"],
        "log_path": str(log_path),
        "stderr_path": str(home / f"{run_id}.stderr.log"),
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "phase": "blocked",
        "attempt": 1,
        "attempt_kind": "dispatch",
        "attempt_started_at": created,
        "created_at": created,
        "budget": budget,
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _manifest(run_id: str) -> str:
    return f"node: {run_id}\nstatus: in-progress\n"


def test_resume_clears_the_superseded_attempt_refusal(crew_home) -> None:
    # A pointer whose prior attempt folded a spend-limit refusal into its
    # budget blocks while it is still the running attempt...
    run_id = "r-resumed-clears-refusal"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True)
    attempt_one_stream = directory / "attempt-1.jsonl"
    attempt_one_stream.write_text('{"type":"turn.started"}\n')
    resume_stream = directory / "resume-1.jsonl"
    resume_stream.write_text('{"type":"turn.started"}\n')
    manifest = directory / "manifest.md"
    manifest.write_text(_manifest(run_id))
    budget = _refusal_budget()
    assert budget.get("refusal") is True
    pointer = _refused_pointer(
        crew_home,
        run_id,
        log_path=attempt_one_stream,
        manifest=manifest,
        budget=budget,
    )
    before = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert before["classification"] == "blocked"
    assert "usage-limit" in before["detail"]

    # ...and once the limit lifts enough to relaunch, the resumed attempt has
    # made no observations of its own, so it must not keep inheriting the fold.
    resumed = crew.record_resumption(
        run_id,
        pid=os.getpid(),
        turn=1,
        log_path=resume_stream,
        stderr_path=directory / "resume-1.stderr.log",
        attempt_started_at=datetime(2026, 9, 5, 1, 0, tzinfo=UTC).isoformat(),
        manifest_baseline_mtime_ns=manifest.stat().st_mtime_ns,
    )

    assert resumed["attempt"] == 2
    assert resumed["phase"] == "working"
    assert resumed["budget"]["refusal"] is False
    assert resumed["budget"]["headroom"] == "unknown"
    after = recovery.classify_pointer(resumed, now_seconds=time.time())
    assert after["classification"] != "blocked"


def test_a_run_refused_and_not_resumed_still_blocks_with_its_reason(
    crew_home,
) -> None:
    # The triage affordance survives: an un-resumed run still surfaces its
    # block with the reason a coordinator needs to decide what to do.
    run_id = "r-refused-not-resumed"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True)
    stream = directory / "stream.jsonl"
    stream.write_text('{"type":"turn.started"}\n')
    manifest = directory / "manifest.md"
    manifest.write_text(_manifest(run_id))
    _refused_pointer(
        crew_home, run_id, log_path=stream, manifest=manifest, budget=_refusal_budget()
    )
    pointer = crew.read_pointer(run_id)

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["classification"] == "blocked"
    assert "refused the turn on a usage-limit" in row["detail"]
    assert "resume" in row["next_action"]


def test_a_resumed_run_whose_new_stream_refuses_blocks_again(crew_home) -> None:
    # The load-bearing half: clearing a stale fold must not disable a live
    # block. A resumed attempt whose own stream records a fresh refusal must
    # land blocked again on that new evidence.
    run_id = "r-resumed-into-a-fresh-refusal"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True)
    attempt_one_stream = directory / "attempt-1.jsonl"
    attempt_one_stream.write_text('{"type":"turn.started"}\n')
    manifest = directory / "manifest.md"
    manifest.write_text(_manifest(run_id))
    _refused_pointer(
        crew_home,
        run_id,
        log_path=attempt_one_stream,
        manifest=manifest,
        budget=_refusal_budget(),
    )

    resumed = crew.record_resumption(
        run_id,
        pid=os.getpid(),
        turn=1,
        log_path=BACKEND_FIXTURES / "codex-usage-limit.jsonl",
        stderr_path=directory / "resume-1.stderr.log",
        attempt_started_at=datetime(2026, 9, 5, 1, 0, tzinfo=UTC).isoformat(),
        manifest_baseline_mtime_ns=manifest.stat().st_mtime_ns,
    )

    assert resumed["budget"]["refusal"] is False
    row = recovery.classify_pointer(resumed, now_seconds=time.time())
    assert row["classification"] == "blocked"
    assert "refused the turn on a usage-limit" in row["detail"]
