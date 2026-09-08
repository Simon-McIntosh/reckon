"""A dispatch backend request is a requirement, never a routing hint."""

from __future__ import annotations

import importlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, ledger

dispatch_module = importlib.import_module("reckon.crew.dispatch")

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "in-harness",
            "model": "default-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "budget_check": True,
        },
        "beta": {
            "launch": "in-harness",
            "model": "requested-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "gamma": {
            "launch": "in-harness",
            "model": "other-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "clive": {
            "launch": "in-harness",
            "model": "local-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "unusable": {
            "launch": "unknown",
            "model": "unusable-model",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    fleet_scripts = root / "skills" / "reckon-ship" / "scripts"
    plans.mkdir(parents=True)
    fleet_scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (fleet_scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "plan-a.html").write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '</head><body><h2 id="dispatch">Dispatch</h2></body></html>',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed repository"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _arguments(repo: Path, *, node: str, dry_run: bool = True) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "plan-a",
        "--section",
        "dispatch",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        node,
        "--goal",
        "record one resolved backend",
        "--done-when",
        "pytest reports nine backend-routing cases passed",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--repo",
        str(repo),
    ]
    if dry_run:
        arguments.append("--dry-run")
    return arguments


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    extra: list[str] | None = None,
    dry_run: bool = True,
    default_backend: str | None = None,
):
    config = deepcopy(CONFIG)
    if default_backend is not None:
        config["default_backend"] = default_backend
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_a, **_k: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [*_arguments(repo, node=node, dry_run=dry_run), *(extra or [])],
    )
    return config, result


def _payload(result) -> dict:
    """Read the documented JSON response ahead of Click's stderr echo."""
    return json.loads(result.output.splitlines()[0])


def _observe_window(
    monkeypatch: pytest.MonkeyPatch, utilisation_pct: float = 52.0
) -> None:
    monkeypatch.setattr(
        dispatch_module,
        "_dispatch_lane_observation",
        lambda *_args, **_kwargs: {
            "headroom": "known",
            "utilisation_pct": utilisation_pct,
            "observed_at": "2026-09-08T19:35:00Z",
        },
    )


def test_explicit_metered_backend_dispatches_without_changing_resolution(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="named-backend",
        extra=["--backend", "beta", "--no-watch"],
        dry_run=False,
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert payload["requested_backend"] == "beta"
    assert payload["backend"] == "beta"
    assert payload["agent"]["backend"] == "beta"
    assert payload["launch"] == "in-harness"
    assert config["default_backend"] == "alpha"


def test_absent_named_backend_refuses_without_an_agent_block(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="absent-backend",
        extra=["--backend", "missing"],
    )

    payload = _payload(result)
    assert result.exit_code == 1
    assert payload["error"] == "dispatch-refused"
    assert "missing" in payload["detail"]
    assert "agent" not in payload


def test_present_but_unusable_named_backend_refuses_with_the_reason(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="unusable-backend",
        extra=["--backend", "unusable"],
    )

    payload = _payload(result)
    assert result.exit_code == 1
    assert payload["error"] == "dispatch-refused"
    assert "unusable" in payload["detail"]
    assert "launch" in payload["detail"]
    assert "agent" not in payload


def test_member_harness_and_named_backend_must_agree(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="beta", root=dispatch_repo)
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="disagreeing-backends",
        extra=["--member", "worker", "--backend", "gamma"],
    )

    payload = _payload(result)
    assert result.exit_code == 1
    assert payload["error"] == "dispatch-refused"
    assert "beta" in payload["detail"]
    assert "gamma" in payload["detail"]
    assert "agent" not in payload


def test_member_backend_without_budget_check_dispatches_without_lane_flag(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="beta", root=dispatch_repo)
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="member-backend-without-budget-check",
        extra=["--member", "worker", "--no-watch"],
        dry_run=False,
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert payload["backend"] == "beta"
    assert payload["launch"] == "in-harness"


def test_in_harness_default_without_budget_check_dispatches_without_lane_flag(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="in-harness-default-without-budget-check",
        extra=["--no-watch"],
        dry_run=False,
        default_backend="gamma",
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert payload["requested_backend"] is None
    assert payload["backend"] == "gamma"
    assert payload["launch"] == "in-harness"


def test_unmetered_member_harness_dispatches_without_a_lane_flag(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="clive", root=dispatch_repo)
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="unmetered-member-backend",
        extra=["--member", "worker", "--no-watch"],
        dry_run=False,
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert payload["requested_backend"] == "clive"
    assert payload["backend"] == "clive"
    assert payload["lane_declaration"]["read_at"].endswith("Z")
    assert {
        key: value
        for key, value in payload["lane_declaration"].items()
        if key != "read_at"
    } == {
        "backend": None,
        "headroom": None,
        "metered": False,
        "observed_at": None,
        "resolved_backend": "clive",
        "utilisation_pct": None,
    }


def test_absent_member_harness_backend_refuses_without_falling_through(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="missing", root=dispatch_repo)
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="absent-member-backend",
        extra=["--member", "worker"],
    )

    payload = _payload(result)
    assert result.exit_code == 1
    assert payload["error"] == "dispatch-refused"
    assert "missing" in payload["detail"]
    assert "agent" not in payload


def test_undeclared_metered_default_refuses_with_window_and_free_alternative(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observe_window(monkeypatch)
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="undeclared-metered-routing",
        dry_run=False,
    )

    payload = _payload(result)
    assert result.exit_code == 2
    assert payload["error"] == "not-dispatchable"
    assert "alpha" in payload["detail"]
    assert "52%" in payload["detail"]
    assert "clive" in payload["detail"]
    assert "--backend alpha" in payload["detail"]
    assert "is metered" in payload["detail"]


def test_unknown_backend_without_budget_check_dispatches_without_lane_flag(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="gamma", root=dispatch_repo)
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="unknown-member-backend-without-budget-check",
        extra=["--member", "worker", "--no-watch"],
        dry_run=False,
    )

    payload = _payload(result)
    assert result.exit_code == 0
    assert payload["requested_backend"] == "gamma"
    assert payload["backend"] == "gamma"
    assert payload["launch"] == "in-harness"


def test_run_record_pairs_declared_lane_with_dispatch_window(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observe_window(monkeypatch)
    _config, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="recorded-backend",
        extra=["--backend", "alpha", "--no-watch"],
        dry_run=False,
    )

    payload = _payload(result)
    pointer = crew.read_pointer(payload["run_id"])
    assert result.exit_code == 0
    assert pointer["requested_backend"] == "alpha"
    assert pointer["backend"] == "alpha"
    assert pointer["agent"]["backend"] == "alpha"
    assert pointer["node"]["requested_backend"] == "alpha"
    expected_lane = {
        "backend": "alpha",
        "headroom": "known",
        "metered": True,
        "observed_at": "2026-09-08T19:35:00Z",
        "resolved_backend": "alpha",
        "utilisation_pct": 52.0,
        "read_at": pointer["lane_declaration"]["read_at"],
    }
    assert expected_lane["read_at"].endswith("Z")
    assert pointer["lane_declaration"] == expected_lane
    assert pointer["node"]["lane_declaration"] == expected_lane
    durable_record = ledger.build_record(
        run_id=pointer["run_id"],
        plan=pointer["node"]["plan"],
        gate="passed",
        node_definition=pointer["node"],
        backend=pointer["backend"],
        agent=pointer["agent"],
    )
    assert durable_record["backend"] == "alpha"
    assert durable_record["node_definition"]["requested_backend"] == "alpha"
    assert durable_record["node_definition"]["lane_declaration"] == expected_lane
