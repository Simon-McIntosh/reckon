"""A worker's flight declaration is its entire MCP server allowlist."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from reckon import _backends

USER_SERVERS = {
    "reckon": {"command": "reckon", "args": ["mcp"]},
    "imas-dd": {"command": "imas-dd"},
    "lucy2": {"command": "lucy2"},
}
PROJECT_SERVERS = {"project-extra": {"command": "project-server"}}


@pytest.fixture
def worker_setup(tmp_path, monkeypatch):
    operator = tmp_path / "operator"
    for folder in (".claude", ".codex", ".omp/agent"):
        (operator / folder).mkdir(parents=True)
    (operator / ".claude.json").write_text(json.dumps({"mcpServers": USER_SERVERS}))
    (operator / ".codex/config.toml").write_text(
        "\n".join(
            f'[mcp_servers."{name}"]\ncommand={json.dumps(server["command"])}'
            for name, server in USER_SERVERS.items()
        )
    )
    (operator / ".omp/agent/mcp.json").write_text(
        json.dumps({"mcpServers": USER_SERVERS})
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".mcp.json").write_text(json.dumps({"mcpServers": PROJECT_SERVERS}))
    (checkout / ".codex").mkdir()
    (checkout / ".codex/config.toml").write_text(
        '[mcp_servers.project-extra]\ncommand="project-server"\n'
    )
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(_backends._CodexDialect, "_inbox_hook_flags", lambda *a: [])
    return operator, checkout


def launch(tmp_path, worker_setup, command, declared=(), anchored=False, **kwargs):
    operator, checkout = worker_setup
    run = tmp_path / command
    run.mkdir(exist_ok=True)
    backend = {"launch": "cli", "command": command, "sandbox": "worktree-full"}
    if command == "omp":
        backend["harness_home_files"] = [{"path": "agent/mcp.json"}]
    if anchored:
        backend["anchored_edit"] = True
    return _backends.launch_plan(
        backend_name=command + ("-anchored" if anchored else ""),
        backend=backend,
        prompt="read the brief and deliver",
        worktree=checkout,
        manifest_path=run / "manifest.md",
        fence_home=operator,
        fence_config={"worker_mcp_servers": list(declared)},
        **kwargs,
    )


def active_servers(plan, worker_setup):
    operator, checkout = worker_setup
    if plan.dialect == "claude":
        assert "--strict-mcp-config" in plan.argv
        path = Path(plan.argv[plan.argv.index("--mcp-config") + 1])
        assert path.stat().st_mode & 0o777 == 0o600
        return set(json.loads(path.read_text())["mcpServers"])
    if plan.dialect == "codex":
        home = Path(plan.environment.get("CODEX_HOME", operator / ".codex"))
        servers = dict(
            tomllib.loads((home / "config.toml").read_text()).get("mcp_servers", {})
        )
        servers.update(
            tomllib.loads((checkout / ".codex/config.toml").read_text())["mcp_servers"]
        )
        for index, word in enumerate(plan.argv[:-1]):
            if word == "-c":
                patch = tomllib.loads(plan.argv[index + 1]).get("mcp_servers", {})
                for name, changes in patch.items():
                    servers[name] = {**servers.get(name, {}), **changes}
        return {name for name, server in servers.items() if server.get("enabled", True)}
    config = json.loads(
        (Path(plan.environment["PI_CODING_AGENT_DIR"]) / "mcp.json").read_text()
    )
    disabled = set(config.get("disabledServers", []))
    return {
        name
        for name, server in config.get("mcpServers", {}).items()
        if name not in disabled and server.get("enabled", True)
    }


@pytest.mark.parametrize("command", ["claude", "clive", "codex", "omp"])
@pytest.mark.parametrize("fence", [True, False])
@pytest.mark.parametrize("resume", [False, True])
def test_undeclared_servers_do_not_reach_launch(
    tmp_path, worker_setup, command, fence, resume
):
    operator, _ = worker_setup
    before = {p: p.read_bytes() for p in operator.rglob("*") if p.is_file()}
    if resume:
        launch(tmp_path, worker_setup, command, fence=fence)
    plan = launch(
        tmp_path,
        worker_setup,
        command,
        fence=fence,
        resume_session="worker-session" if resume else None,
    )
    assert active_servers(plan, worker_setup) == set()
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("command", ["claude", "clive", "codex", "omp"])
@pytest.mark.parametrize("declared", [("imas-dd",), ("reckon", "lucy2")])
def test_only_declared_user_servers_are_available(
    tmp_path, worker_setup, command, declared
):
    plan = launch(tmp_path, worker_setup, command, declared=declared)
    assert active_servers(plan, worker_setup) == set(declared)


@pytest.mark.parametrize("command", ["claude", "clive", "codex"])
def test_declared_project_server_is_available(tmp_path, worker_setup, command):
    plan = launch(tmp_path, worker_setup, command, declared=["project-extra"])
    assert active_servers(plan, worker_setup) == {"project-extra"}


@pytest.mark.parametrize("command", ["claude", "clive", "codex"])
def test_anchored_backend_keeps_only_its_edit_server(tmp_path, worker_setup, command):
    plan = launch(tmp_path, worker_setup, command, anchored=True)
    expected = "anchored" if command == "codex" else "reckon-anchored-edit"
    assert active_servers(plan, worker_setup) == {expected}


def test_profile_only_servers_do_not_create_incomplete_root_transports(tmp_path):
    from reckon._worker_fence import codex_worker_mcp_flags

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        'profile="study"\n[profiles.study.mcp_servers.extra]\ncommand="server"\n'
    )
    flags = codex_worker_mcp_flags(home, tmp_path / "checkout")
    assert tomllib.loads(flags[1])["mcp_servers"] == {}
    assert tomllib.loads(flags[3])["profiles"]["study"]["mcp_servers"] == {
        "extra": {"enabled": False}
    }
