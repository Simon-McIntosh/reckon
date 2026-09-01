"""Hermetic contracts for the shared project watch event stream.

The stream stores transition objects rather than rendered lines, because a
rendered line cannot say which session owns the run and a reader that cannot
answer that cannot filter to its own fleet. These checks therefore read the
data and render it the way a follower does.
"""

from __future__ import annotations

import re
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep watcher registrations, pointers, and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _set_phase(run_id: str, phase: str) -> None:
    pointer = crew.read_pointer(run_id)
    pointer["phase"] = phase
    crew._write_json(crew.pointer_path(run_id), pointer)


def _deliver(home: Path, run_id: str, status: str, *, reason: str = "") -> None:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: stream-node\nstatus: {status}\ncommits: HEAD\n"
        f"blockers: {reason or 'none'}\n"
    )


def _read_new(handle) -> list[str]:
    """Render whatever the stream has produced since this handle last read."""
    return [
        recovery.format_watch_transition(runs.parse_stream_line(line))
        for line in handle.read().splitlines()
        if line.strip()
    ]


def test_successive_arms_share_one_producer_and_stream(home) -> None:
    _write_pointer(home, "r-first", "first-node", phase="starting")
    _write_pointer(home, "r-second", "second-node", phase="working")

    with runs._project_watch_claim("proj", "1h") as (first, registration):
        assert first is True
        stream_path = Path(registration["stream_path"])
        with runs._project_watch_claim("proj", "1h") as (second, live_seat):
            assert second is False
            assert live_seat["pid"] == registration["pid"]
            assert Path(live_seat["stream_path"]) == stream_path

        with ExitStack() as stack:
            readers = [
                stack.enter_context(stream_path.open(encoding="utf-8"))
                for _ in range(2)
            ]
            baselines = [_read_new(reader) for reader in readers]
            assert baselines[0] == baselines[1]
            assert len(baselines[0]) == 2
            assert all(
                "2 working · 0 blocked · 0 unpromoted" in line for line in baselines[0]
            )

            _set_phase("r-first", "working")
            crew.list_live(project="proj")
            transitions = [_read_new(reader) for reader in readers]

        assert transitions[0] == transitions[1]
        assert len(transitions[0]) == 1
        assert "first-node" in transitions[0][0]
        assert "dispatched → working" in transitions[0][0]


def test_transition_appends_once_and_reader_restart_from_end_is_quiet(home) -> None:
    _write_pointer(home, "r-only", "only-node", phase="working")

    with runs._project_watch_claim("proj", "1h") as (_acquired, registration):
        stream_path = Path(registration["stream_path"])
        _deliver(
            home,
            "r-only",
            "blocked",
            reason="dependency unavailable; retry after configuration changes",
        )
        crew.list_live(project="proj")
        first_size = stream_path.stat().st_size
        crew.list_live(project="proj")
        assert stream_path.stat().st_size == first_size

        with stream_path.open(encoding="utf-8") as restarted:
            restarted.seek(0, 2)
            crew.list_live(project="proj")
            assert restarted.read() == ""

        events = list(runs.read_stream_events(stream_path))

    lines = [recovery.format_watch_transition(event) for event in events]
    assert len(lines) == 2
    assert sum("working → blocked" in line for line in lines) == 1
    assert lines[-1].endswith("· dependency unavailable")
    # clock, then the agent column, then the node
    assert re.match(r"^\d{2}:\d{2}:\d{2}\s+\S*\s*only-node", lines[-1])
    assert "0 working · 1 blocked · 0 unpromoted" in lines[-1]
    assert events[-1]["run_id"] == "r-only"


def test_late_reader_gets_current_baseline_and_only_future_lines(home) -> None:
    _write_pointer(home, "r-only", "only-node", phase="starting")

    with runs._project_watch_claim("proj", "1h") as (_acquired, registration):
        stream_path = Path(registration["stream_path"])
        _set_phase("r-only", "working")
        crew.list_live(project="proj")
        _deliver(home, "r-only", "blocked", reason="waiting for configuration")
        crew.list_live(project="proj")

        cursor = runs.watch_stream_cursor("proj")
        assert len(cursor["baseline"]) == 1
        assert cursor["baseline"][0]["to_state"] == "blocked"

        _deliver(home, "r-only", "complete")
        crew.list_live(project="proj")
        with stream_path.open(encoding="utf-8") as late_reader:
            late_reader.seek(cursor["offset"])
            subsequent = _read_new(late_reader)

    assert len(subsequent) == 1
    assert "blocked → complete" in subsequent[0]
    assert "dispatched → working" not in subsequent[0]
    assert "working → blocked" not in subsequent[0]
    assert "0 working · 0 blocked · 1 unpromoted" in subsequent[0]
