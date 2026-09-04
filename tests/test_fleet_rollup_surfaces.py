"""Parity checks for the HTTP, CLI, and MCP fleet-rollup readers."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from reckon import cli, crew, mcp, serve


def _plan_html(project: str, slug: str, status: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{project}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="{status}">
<title>{slug}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _project(tmp_path: Path, name: str, *, commit_age_days: int) -> Path:
    repo = tmp_path / name
    docs = repo / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    plan = plans / "work.html"
    plan.write_text(_plan_html(name, "work", "active"), encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    stamp = (datetime.now(tz=UTC) - timedelta(days=commit_age_days)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    subprocess.run(
        ["git", "-C", str(repo), "add", str(plan.relative_to(repo))],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return docs


def _served_rows(payload: dict) -> list[dict]:
    return [entry["data"]["projects"][0] for entry in payload["projects"]]


def test_all_fleet_readers_return_identical_compact_rows(tmp_path, monkeypatch):
    recent = _project(tmp_path, "recent", commit_age_days=1)
    quiet = _project(tmp_path, "quiet", commit_age_days=90)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(
        json.dumps({"quiet": str(quiet), "recent": str(recent)}),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)

    def live_rows(*, project=None):
        if project == "recent":
            return [{"run_id": "private-run", "follower": "private-record"}]
        return []

    monkeypatch.setattr(crew, "list_live", live_rows)

    mounted = serve.load_mounts()
    served = _served_rows(serve.collect_projects(mounted))
    command = CliRunner().invoke(cli.main, ["fleet"])
    mcp_view = mcp._crew(view="fleet")

    assert command.exit_code == 0, command.output
    cli_view = json.loads(command.output)
    assert cli_view["projects"] == served
    assert mcp_view["projects"] == served
    assert {row["project"] for row in served} == {"quiet", "recent"}
    assert next(row for row in served if row["project"] == "quiet")["activity30"] == []
    assert set(mcp_view) == {"ok", "view", "projects"}
    assert "private-run" not in json.dumps(mcp_view)
    assert "private-record" not in json.dumps(mcp_view)


def test_fleet_cli_refuses_when_no_projects_are_mounted(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts.json"
    mounts.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)

    result = CliRunner().invoke(cli.main, ["fleet"])

    assert result.exit_code != 0
    assert "no projects are mounted" in result.output


def test_fleet_rollup_remains_a_view_on_the_existing_mcp_tool():
    names = {item.name for item in mcp.mcp._tool_manager.list_tools()}

    assert names == {"_read_plan", "_edit_plan", "_roadmap", "_audit", "_crew"}
