"""Execution evidence must fit the role selected for a dispatch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew


CONFIG = {
    "default_backend": "native",
    "backends": {
        "native": {
            "launch": "in-harness",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {
        "review": {"execution_capable": False, "sandbox": "read-only"},
        "test": {"execution_capable": True},
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)
    return config_home


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text()
    )
    (root / "docs" / "plans" / "dispatch.html").write_text(
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="dispatch">'
        '<h2 id="execution">Execution fit</h2>'
    )
    (root / "seed.txt").write_text("seed\n")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/dispatch.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def _dispatch_arguments(
    *, node_id: str, role: str, repository: Path, manifest: Path, session: str
) -> list[str]:
    return [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "dispatch",
        "--section",
        "execution",
        "--role",
        role,
        "--node",
        node_id,
        "--goal",
        "verify one runtime route",
        "--done-when",
        "running a test suite reports 42 passed and 0 failures",
        "--write-path",
        f"records/{node_id}.json",
        "--time-budget",
        "20m",
        "--manifest",
        str(manifest),
        "--session",
        session,
        "--repo",
        str(repository),
    ]


def test_execution_measure_requires_a_capable_role_or_recorded_override(
    isolated_home: Path, repository: Path
) -> None:
    runner = CliRunner()
    refused = runner.invoke(
        cli_module.main,
        _dispatch_arguments(
            node_id="refused-node",
            role="review",
            repository=repository,
            manifest=isolated_home / "refused.md",
            session="refused-session",
        ),
    )
    refusal = json.loads(refused.output)
    assert refused.exit_code == 2
    assert refusal["error"] == "not-dispatchable"
    assert "running a test suite" in refusal["detail"]
    assert "role 'review'" in refusal["detail"]
    assert crew.list_live() == []

    capable = runner.invoke(
        cli_module.main,
        _dispatch_arguments(
            node_id="capable-node",
            role="test",
            repository=repository,
            manifest=isolated_home / "capable.md",
            session="capable-session",
        ),
    )
    capable_record = json.loads(capable.output)
    assert capable.exit_code == 0
    assert capable_record["execution_fit"] == {
        "allowed": True,
        "execution_capable": True,
        "matched_measure": "running a test suite",
        "override": False,
        "role": "test",
        "status": "compatible",
    }

    override_args = _dispatch_arguments(
        node_id="override-node",
        role="review",
        repository=repository,
        manifest=isolated_home / "override.md",
        session="override-session",
    )
    override_args.append("--allow-execution-mismatch")
    overridden = runner.invoke(cli_module.main, override_args)
    overridden_record = json.loads(overridden.output)
    assert overridden.exit_code == 0
    assert overridden_record["execution_fit"] == {
        "allowed": True,
        "execution_capable": False,
        "matched_measure": "running a test suite",
        "override": True,
        "role": "review",
        "status": "overridden",
    }
    persisted = json.loads(crew.pointer_path(overridden_record["run_id"]).read_text())
    assert persisted["execution_fit"] == overridden_record["execution_fit"]

    completed = crew.complete(
        overridden_record["run_id"],
        gate="passed",
        outcome="the deliberate exception completed",
        root=repository,
    )
    assert completed["record"]["execution_fit"] == overridden_record["execution_fit"]
