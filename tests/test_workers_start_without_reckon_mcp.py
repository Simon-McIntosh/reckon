"""Dispatched harnesses exclude the coordination server from worker MCP."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

from reckon import _backends


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    operator = tmp_path / "operator"
    for folder in (".claude", ".codex", ".omp/agent"):
        (operator / folder).mkdir(parents=True)
    servers = {
        "reckon": {"command": "reckon", "args": ["mcp"]},
        "useful": {"command": "useful-server"},
    }
    (operator / ".claude.json").write_text(json.dumps({"mcpServers": servers}))
    (operator / ".codex/config.toml").write_text(
        '[mcp_servers.reckon]\ncommand = "reckon"\nargs = ["mcp"]\n'
        '[mcp_servers.useful]\ncommand = "useful-server"\n'
    )
    (operator / ".omp/agent/mcp.json").write_text(json.dumps({"mcpServers": servers}))
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(crew))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(_backends._CodexDialect, "_inbox_hook_flags", lambda *a: [])
    return operator


def _launch(tmp_path, operator, command, **kwargs):
    run = tmp_path / command
    run.mkdir(exist_ok=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    (checkout / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"reckon": {"command": "reckon", "args": ["mcp"]}}})
    )
    backend = {"launch": "cli", "command": command, "sandbox": "worktree-full"}
    if command == "omp":
        backend["harness_home_files"] = [{"path": "agent/mcp.json"}]
    return _backends.launch_plan(
        backend_name=command,
        backend=backend,
        prompt="read the brief and deliver",
        worktree=str(checkout),
        manifest_path=str(run / "manifest.md"),
        fence_home=operator,
        fence_config={"worker_mcp_servers": ["useful"]},
        **kwargs,
    )


@pytest.mark.parametrize("command", ["claude", "clive", "codex", "omp"])
@pytest.mark.parametrize("fence", [True, False])
def test_worker_launch_excludes_reckon(tmp_path, worker_home, command, fence):
    before = {p: p.read_bytes() for p in worker_home.rglob("*") if p.is_file()}
    plan = _launch(tmp_path, worker_home, command, fence=fence)
    if command in ("claude", "clive"):
        if fence:
            home = Path(plan.environment["CLAUDE_CONFIG_DIR"])
            carried = json.loads((home / ".claude.json").read_text())
            assert "reckon" not in carried["mcpServers"], carried["mcpServers"]
            assert "useful" in carried["mcpServers"]
        assert "--strict-mcp-config" in plan.argv
        config_path = Path(plan.argv[plan.argv.index("--mcp-config") + 1])
        assert config_path.stat().st_mode & 0o777 == 0o600
        explicit = json.loads(config_path.read_text())
        assert "reckon" not in explicit["mcpServers"]
        assert "useful" in explicit["mcpServers"]
    elif command == "codex":
        assert any(
            tomllib.loads(plan.argv[i + 1])
            .get("mcp_servers", {})
            .get("reckon", {})
            .get("enabled")
            is False
            for i, value in enumerate(plan.argv[:-1])
            if value == "-c"
        )
        home = Path(plan.environment["CODEX_HOME"]) if fence else worker_home / ".codex"
        assert "mcp_servers.useful" in (home / "config.toml").read_text()
    else:
        overlay = next(
            v.split("=", 1)[1] for v in plan.argv if v.startswith("--config=")
        )
        assert (
            yaml.safe_load(Path(overlay).read_text())["mcp"]["enableProjectConfig"]
            is False
        )
        config = json.loads(
            (Path(plan.environment["PI_CODING_AGENT_DIR"]) / "mcp.json").read_text()
        )
        assert "reckon" not in config["mcpServers"]
        assert "reckon" in config["disabledServers"]
        assert "useful" in config["mcpServers"]
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("command", ["claude", "clive", "codex", "omp"])
def test_resumed_worker_still_excludes_reckon(tmp_path, worker_home, command):
    _launch(tmp_path, worker_home, command)
    plan = _launch(tmp_path, worker_home, command, resume_session="worker-session")
    if command in ("claude", "clive"):
        explicit = json.loads(
            Path(plan.argv[plan.argv.index("--mcp-config") + 1]).read_text()
        )
        assert "reckon" not in explicit["mcpServers"]
    elif command == "codex":
        assert any(
            tomllib.loads(plan.argv[i + 1])
            .get("mcp_servers", {})
            .get("reckon", {})
            .get("enabled")
            is False
            for i, value in enumerate(plan.argv[:-1])
            if value == "-c"
        )
    else:
        config = json.loads(
            (Path(plan.environment["PI_CODING_AGENT_DIR"]) / "mcp.json").read_text()
        )
        assert "reckon" in config["disabledServers"]


@pytest.mark.parametrize(
    "transport", ["", 'command="reckon"', 'url="https://example.invalid/mcp"']
)
def test_codex_override_keeps_a_valid_transport(tmp_path, transport):
    from reckon._worker_fence import codex_worker_mcp_flags

    home = tmp_path / "home"
    home.mkdir()
    if transport:
        (home / "config.toml").write_text("[mcp_servers.reckon]\n" + transport + "\n")
    flags = codex_worker_mcp_flags(home, tmp_path / "checkout")
    servers = tomllib.loads(flags[1])["mcp_servers"]
    assert servers == ({"reckon": {"enabled": False}} if transport else {})
