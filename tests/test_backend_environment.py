from __future__ import annotations

import copy
import importlib
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.flight import FlightConfigError, flight_report, resolve
from tests.test_crew import CONFIG, _node

pytest_plugins = ("tests.test_crew",)

dispatch_module = importlib.import_module("reckon.crew.dispatch")


def _host_layer(path: Path, token: str = "${DISPATCH_TOKEN}") -> Path:
    path.write_text(
        """backends:
  endpoint:
    launch: cli
    command: codex
    model: local-model
    effort: high
    sandbox: worktree-full
    environment:
      API_BASE: https://endpoint.invalid/api
      API_TOKEN: """
        + token
        + """
roles:
  implement:
    backend: endpoint
"""
    )
    return path


def test_backend_environment_resolves_values_and_per_key_origins(
    tmp_path: Path,
) -> None:
    resolved = resolve(
        host_path=_host_layer(tmp_path / "flight.yaml", "literal-token"),
        project_path=tmp_path / "missing-project.yaml",
    )

    environment = resolved.config["backends"]["endpoint"]["environment"]
    assert environment == {
        "API_BASE": "https://endpoint.invalid/api",
        "API_TOKEN": "literal-token",
    }
    assert resolved.origin("backends.endpoint.environment.API_BASE") == "host"
    assert resolved.origin("backends.endpoint.environment.API_TOKEN") == "host"


def test_unselected_unset_reference_is_reported_without_blocking_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ABSENT_DISPATCH_TOKEN", raising=False)
    host = tmp_path / "flight.yaml"
    host.write_text(
        """backends:
  unavailable:
    launch: cli
    command: python3
    environment:
      API_TOKEN: ${ABSENT_DISPATCH_TOKEN}
  available:
    launch: cli
    command: python3
    environment:
      API_TOKEN: literal-token
"""
    )

    report = flight_report(
        host_path=host,
        project_path=tmp_path / "missing-project.yaml",
    )

    assert report["config"]["backends"]["unavailable"]["environment"] == {
        "API_TOKEN": "${ABSENT_DISPATCH_TOKEN}"
    }
    unavailable = report["availability"]["unavailable"]
    absent_command_shape = {
        "launch",
        "command",
        "command_found",
        "command_path",
        "authenticated",
        "detail",
    }
    assert set(unavailable) == absent_command_shape
    assert "API_TOKEN" in unavailable["detail"]
    assert "ABSENT_DISPATCH_TOKEN" in unavailable["detail"]
    assert report["config"]["backends"]["available"]["environment"] == {
        "API_TOKEN": "literal-token"
    }


def test_selected_unset_reference_is_a_typed_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ABSENT_DISPATCH_TOKEN", raising=False)
    backend = {
        "launch": "cli",
        "command": "codex",
        "environment": {"API_TOKEN": "${ABSENT_DISPATCH_TOKEN}"},
    }

    with pytest.raises(FlightConfigError) as excinfo:
        _backends.launch_plan(
            backend_name="unavailable",
            backend=backend,
            prompt="perform the bounded task",
            worktree="/tmp/worktree",
        )

    assert excinfo.value.key_path == "backends.unavailable.environment.API_TOKEN"
    assert "ABSENT_DISPATCH_TOKEN" in excinfo.value.constraint
    assert "unset environment variable" in excinfo.value.constraint


def test_launch_plan_expands_and_carries_selected_backend_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPATCH_TOKEN", "secret-from-dispatcher")
    plan = _backends.launch_plan(
        backend_name="endpoint",
        backend={
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "environment": {
                "API_BASE": "https://endpoint.invalid/api",
                "API_TOKEN": "${DISPATCH_TOKEN}",
            },
        },
        prompt="perform the bounded task",
        worktree="/tmp/worktree",
    )

    assert plan.environment == {
        "API_BASE": "https://endpoint.invalid/api",
        "API_TOKEN": "secret-from-dispatcher",
    }


def test_spawn_merges_declared_environment_over_inherited_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", "/inherited/bin")
    monkeypatch.setenv("HOME", "/inherited/home")
    captured: dict[str, object] = {}

    class Spawned:
        pid = 321

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return Spawned()

    monkeypatch.setattr(dispatch_module.subprocess, "Popen", popen)
    plan = _backends.LaunchPlan(
        backend="endpoint",
        dialect="codex",
        argv=["codex", "exec"],
        cwd=str(tmp_path),
        stdin_text="",
        environment={"PATH": "/declared/bin", "API_BASE": "https://endpoint.invalid"},
        final_message_path=None,
        resumed_session=None,
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("prompt")

    pid = dispatch_module._spawn(
        plan,
        log_path=tmp_path / "worker.log",
        stderr_path=tmp_path / "worker.stderr.log",
        prompt_path=prompt,
    )

    assert pid == 321
    environment = captured["env"]
    assert environment["PATH"] == "/declared/bin"
    assert environment["HOME"] == "/inherited/home"
    assert environment["API_BASE"] == "https://endpoint.invalid"


def test_run_record_agent_configuration_excludes_environment(home, repo) -> None:
    launched: dict[str, object] = {}
    config = copy.deepcopy(CONFIG)
    config["backends"]["alpha"]["environment"] = {"API_TOKEN": "must-not-enter-ledger"}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched["plan"] = plan
        log_path.write_text("")
        return 4242

    record = crew.dispatch(
        node=_node(manifest_path=""),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=launcher,
    )

    assert launched["plan"].environment == {"API_TOKEN": "must-not-enter-ledger"}
    assert record["agent"] == {
        "backend": "alpha",
        "launch": "cli",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
    }
    assert set(record["agent"]) == {"backend", "launch", "model", "effort", "sandbox"}
    assert "environment" not in record
