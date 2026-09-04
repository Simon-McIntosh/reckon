"""The live coordinator directory is a hermetic read over dispatch pointers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from reckon import _store, crew, mcp
from reckon.cli import main as cli_main


def _pointer(
    home: Path,
    *,
    run_id: str,
    session: str,
    project: str,
    repository: str,
    node: str,
    plan: str,
    alive: bool,
    socket: str | None = None,
) -> None:
    log = home / "crew" / "runs" / run_id / "stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    event = {"type": "system", "subtype": "init"}
    if socket is not None:
        event["messaging_socket_path"] = socket
    log.write_text(json.dumps(event) + "\n")
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "session": session,
            "project": project,
            "repo": repository,
            "worktree": f"{repository}/.reckon-worktrees/{session}/{node}",
            "node": {"id": node, "plan": plan, "section": "delivery"},
            "phase": "working" if alive else "complete",
            "process_alive": alive,
            "log_path": str(log),
            "manifest_path": str(log.parent / "manifest.md"),
        },
    )


def _directory_snapshot(path: Path) -> dict[str, int]:
    if not path.is_dir():
        return {}
    return {item.name: item.stat().st_mtime_ns for item in path.glob("*.json")}


def _seed_directory(tmp_path: Path, monkeypatch) -> tuple[Path, Path, dict[str, int]]:
    real_home = _store._config_home()
    real_live = real_home / "crew" / "live"
    before = _directory_snapshot(real_live)
    home = tmp_path / "isolated-config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _pointer(
        home,
        run_id="run-physics",
        session="physics-coordinator",
        project="nova",
        repository="/repos/nova",
        node="solve-boundary",
        plan="equilibrium",
        alive=True,
        socket="/run/observed/physics.sock",
    )
    _pointer(
        home,
        run_id="run-render",
        session="render-coordinator",
        project="nova",
        repository="/repos/nova",
        node="render-section",
        plan="geometry",
        alive=False,
    )
    _pointer(
        home,
        run_id="run-catalog",
        session="catalog-coordinator",
        project="names",
        repository="/repos/names",
        node="check-catalog",
        plan="catalog-integrity",
        alive=True,
    )
    assert _directory_snapshot(real_live) == before
    return home, real_live, before


def test_one_read_names_every_coordinator_across_repositories(
    tmp_path, monkeypatch
) -> None:
    _home, real_live, before = _seed_directory(tmp_path, monkeypatch)

    result = mcp._crew(view="directory")

    assert result["coordinator_count"] == 3
    assert result["live_node_count"] == 2
    assert result["unowned_run_count"] == 0
    assert [(row["project"], row["session"]) for row in result["coordinators"]] == [
        ("names", "catalog-coordinator"),
        ("nova", "physics-coordinator"),
        ("nova", "render-coordinator"),
    ]
    nova = [row for row in result["coordinators"] if row["project"] == "nova"]
    assert [row["repository"] for row in nova] == ["/repos/nova", "/repos/nova"]
    assert [row["plans"] for row in nova] == [["equilibrium"], ["geometry"]]
    assert [row["live_node_count"] for row in nova] == [1, 0]
    assert _directory_snapshot(real_live) == before


def test_project_and_human_node_resolve_the_right_owner(tmp_path, monkeypatch) -> None:
    _seed_directory(tmp_path, monkeypatch)

    project = mcp._crew("nova", view="directory")
    node = mcp._crew(view="directory", node="render-section")
    run = mcp._crew(view="directory", run_id="run-physics")

    assert project["coordinator_count"] == 2
    assert node["resolved"] == {
        "by": "node",
        "value": "render-section",
        "run_id": "run-render",
        "node": "render-section",
        "session": "render-coordinator",
    }
    assert run["resolved"]["session"] == "physics-coordinator"


def test_activity_and_transport_are_reported_only_from_observation(
    tmp_path, monkeypatch
) -> None:
    _seed_directory(tmp_path, monkeypatch)

    result = mcp._crew("nova", view="directory")
    rows = {row["session"]: row for row in result["coordinators"]}

    assert rows["physics-coordinator"]["state"] == "dispatching"
    assert rows["render-coordinator"]["state"] == "all-terminal"
    assert rows["physics-coordinator"]["transports"][0]["address"] == (
        "/run/observed/physics.sock"
    )
    assert "transports" not in rows["render-coordinator"]
    assert "transport" not in rows["render-coordinator"]["runs"][0]


def test_directory_probes_dispatch_pids_before_classifying_runs(
    tmp_path, monkeypatch
) -> None:
    real_home = _store._config_home()
    real_live = real_home / "crew" / "live"
    before = _directory_snapshot(real_live)
    home = tmp_path / "isolated-config"
    monkeypatch.setenv("RECKON_HOME", str(home))

    live_dir = home / "crew" / "runs" / "run-live"
    live_dir.mkdir(parents=True)
    manifest = live_dir / "manifest.md"
    manifest.write_text("node: run-live\nstatus: blocked\nblockers: still working\n")
    log = live_dir / "stream.jsonl"
    log.write_text('{"type":"assistant","message":"working"}\n')
    older = log.stat().st_mtime_ns - 1_000_000
    os.utime(manifest, ns=(older, older))
    crew._write_json(
        crew.pointer_path("run-live"),
        {
            "run_id": "run-live",
            "session": "mixed-coordinator",
            "project": "nova",
            "repo": "/repos/nova",
            "worktree": "/repos/nova/.reckon-worktrees/mixed/run-live",
            "node": {"id": "live-node", "plan": "equilibrium"},
            "phase": "working",
            "pid": os.getpid(),
            "process_alive": None,
            "log_path": str(log),
            "manifest_path": str(manifest),
        },
    )
    crew._write_json(
        crew.pointer_path("run-dead"),
        {
            "run_id": "run-dead",
            "session": "mixed-coordinator",
            "project": "nova",
            "repo": "/repos/nova",
            "worktree": "/repos/nova/.reckon-worktrees/mixed/run-dead",
            "node": {"id": "dead-node", "plan": "equilibrium"},
            "phase": "working",
            "pid": 2_147_483_647,
            "process_alive": True,
            "log_path": str(home / "crew" / "runs" / "run-dead" / "stream.jsonl"),
            "manifest_path": str(home / "crew" / "runs" / "run-dead" / "manifest.md"),
        },
    )

    result = mcp._crew("nova", view="directory")
    coordinator = result["coordinators"][0]
    rows = {row["run_id"]: row for row in coordinator["runs"]}

    assert rows["run-live"]["process_alive"] is True
    assert rows["run-live"]["classification"] == "running"
    assert rows["run-dead"]["process_alive"] is False
    assert rows["run-dead"]["classification"] == "abandoned"
    assert coordinator["state"] == "dispatching"
    assert coordinator["live_node_count"] == 1
    assert result["live_node_count"] == 1

    crew.observe("run-live")
    crew.observe("run-dead")
    live_rows = {row["run_id"]: row for row in mcp._crew("nova", view="live")["runs"]}
    assert {
        run_id: (row["classification"], row["process_alive"])
        for run_id, row in rows.items()
    } == {
        run_id: (row["classification"], row["process_alive"])
        for run_id, row in live_rows.items()
    }
    assert _directory_snapshot(real_live) == before


def test_directory_is_a_cli_verb_and_an_existing_mcp_view(
    tmp_path, monkeypatch
) -> None:
    _seed_directory(tmp_path, monkeypatch)

    command = CliRunner().invoke(
        cli_main, ["crew", "directory", "--project", "nova", "--node", "solve-boundary"]
    )

    assert command.exit_code == 0, command.output
    assert json.loads(command.output)["resolved"]["session"] == "physics-coordinator"
    assert {item.name for item in mcp.mcp._tool_manager.list_tools()} == {
        "_read_plan",
        "_edit_plan",
        "_roadmap",
        "_audit",
        "_crew",
    }


def test_a_sessionless_pointer_is_visible_but_is_not_a_coordinator(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "isolated-config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    crew._write_json(
        crew.pointer_path("run-unowned"),
        {
            "run_id": "run-unowned",
            "project": "nova",
            "phase": "complete",
            "process_alive": False,
            "node": {"id": "orphan", "plan": "equilibrium"},
        },
    )

    result = mcp._crew(view="directory", node="orphan")

    assert result["coordinator_count"] == 0
    assert result["unowned_run_count"] == 1
    assert result["resolved"]["session"] is None


def test_ship_guidance_makes_cross_repository_findings_collaborative() -> None:
    skill = (
        Path(__file__).parents[1] / "skills" / "reckon-ship" / "SKILL.md"
    ).read_text()
    prose = " ".join(skill.split())

    assert 'crew(view="directory")' in skill
    assert "reports the finding to the live session working there" in prose
    assert (
        "Send it as a finding, never as an instruction and never as authority" in prose
    )
    assert "mcp__reckon___crew" in skill
