"""The crew runs view forwards a run id through the MCP tool.

``runs_view`` narrows a read by run id, but the MCP ``crew(view="runs")``
bridge dropped the argument, so a caller asking for one run still walked every
pointer in the fleet. These tests call the tool's ``_crew`` entry point against
a synthetic configuration home holding two live runs: asking for one run must
return that run's row alone, and the run id must arrive at ``runs_view`` rather
than being matched afterwards.
"""

from __future__ import annotations

from pathlib import Path

from reckon import crew, mcp

PROJECT = "alpha"
TARGET = "r-target-run"
OTHER = "r-other-run"


def _write_live(home: Path, run_id: str, *, node: str) -> None:
    worktree = home / "worktrees" / run_id
    worktree.mkdir(parents=True)
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(home / "repository"),
            "node": {"id": node, "plan": "run-query"},
            "phase": "working",
            "process_alive": True,
            "session_id": "session-a",
            "member": "member-a",
            "worktree": str(worktree),
        },
    )


def _two_live_runs(home: Path) -> None:
    _write_live(home, TARGET, node="node-target")
    _write_live(home, OTHER, node="node-other")


def test_runs_view_returns_only_the_named_run(isolated_reckon_home: Path) -> None:
    _two_live_runs(isolated_reckon_home)

    whole = mcp._crew(PROJECT, view="runs", source="live")
    assert sorted(row["run_id"] for row in whole["rows"]) == sorted([TARGET, OTHER])

    result = mcp._crew(PROJECT, view="runs", source="live", run_id=TARGET)

    assert result["count"] == 1
    assert [row["run_id"] for row in result["rows"]] == [TARGET]
    assert result["rows"][0]["node"] == "node-target"


def test_run_id_reaches_runs_view(isolated_reckon_home: Path, monkeypatch) -> None:
    _two_live_runs(isolated_reckon_home)
    seen: dict[str, object] = {}

    def spy(project, **kwargs):
        seen["project"] = project
        seen.update(kwargs)
        return {"ok": True, "view": "runs", "rows": [], "count": 0}

    monkeypatch.setattr(mcp, "crew_runs_view", spy)

    result = mcp._crew(PROJECT, view="runs", source="live", run_id=TARGET)

    assert result["ok"] is True
    assert seen["run_id"] == TARGET
    assert seen["project"] == PROJECT
