"""A shared backend stop has one fleet verdict while independent ends stay separate."""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import query, recovery
from reckon.crew.runs import list_live


def _terminal(
    home: Path, run_id: str, backend: str, ended_at: str, *, model_error: bool = True
) -> None:
    run_dir = home / "crew" / "runs" / run_id
    run_dir.mkdir(parents=True)
    stream = run_dir / "stream.jsonl"
    stream.write_text(
        json.dumps(
            {
                "type": "result",
                "timestamp": ended_at,
                "subtype": "success",
                "is_error": True,
                "result": (
                    "there is an issue with the selected model — it may not exist "
                    "or you may not have access to it"
                    if model_error
                    else "worker failed"
                ),
            }
        )
        + "\n"
    )
    (run_dir / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "recorded_by": "supervisor",
                "worker_pid": 4242,
                "launched_at": "2026-09-14T06:40:00Z",
                "exited_at": ended_at,
                "exit_code": 1,
                "signal": None,
                "stream_records_seen": 1,
                "last_record_type": "result",
                "ended_during": "working",
            }
        )
    )
    worktree = home / "trees" / run_id
    worktree.mkdir(parents=True)
    pointer = {
        "run_id": run_id,
        "project": "fixture-project",
        "phase": "failed",
        "process_alive": True,
        "launcher_host": "another-login-node",
        "launch": "cli",
        "backend": backend,
        "session_id": f"session-{run_id}",
        "log_path": str(stream),
        "manifest_path": str(run_dir / "manifest.md"),
        "worktree": str(worktree),
        "node": {"id": run_id, "project": "fixture-project"},
    }
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / f"{run_id}.json").write_text(json.dumps(pointer))


def test_simultaneous_terminals_are_one_lane_event(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    for run_id, backend, ended_at in (
        ("r-first", "shared", "2026-09-14T06:52:19Z"),
        ("r-second", "shared", "2026-09-14T06:52:23Z"),
        ("r-third", "shared", "2026-09-14T06:52:28Z"),
        ("r-other-backend", "other", "2026-09-14T06:52:24Z"),
        ("r-later", "shared", "2026-09-14T07:00:20Z"),
    ):
        _terminal(tmp_path, run_id, backend, ended_at)

    pointers = list_live(project="fixture-project")
    rows = query.project_live_rows(pointers)
    events = [row for row in rows if row["classification"] == "lane-event"]
    assert len(events) == 1
    assert events[0]["lane_event"]["backend"] == "shared"
    assert set(events[0]["lane_event"]["run_ids"]) == {"r-first", "r-second", "r-third"}
    assert events[0]["lane_event"]["cause"] == "backend-catalog-change"
    assert "selected model" in events[0]["lane_event"]["reason"]
    assert {row["run_id"] for row in rows if row["classification"] != "lane-event"} == {
        "r-other-backend",
        "r-later",
    }

    single = query.project_live_rows(
        [next(p for p in pointers if p["run_id"] == "r-first")]
    )
    assert len(single) == 1
    assert single[0]["classification"] != "lane-event"
    assert single[0]["lane_cause"]["kind"] == "backend-catalog-change"

    compact = query.runs_view("fixture-project", source="live")
    assert compact["count"] == 3
    assert sum(row["classification"] == "lane-event" for row in compact["rows"]) == 1


def test_stderr_model_warning_is_not_a_terminal_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    _terminal(
        tmp_path,
        "r-healthy-warning",
        "shared",
        "2026-09-14T06:52:19Z",
        model_error=False,
    )
    run_dir = tmp_path / "crew" / "runs" / "r-healthy-warning"
    (run_dir / "stderr.log").write_text("there is an issue with the selected model\n")
    row = query.project_live_rows(list_live(project="fixture-project"))[0]
    assert row["lane_cause"] is None
