"""A dispatch dry run shows how each --set override resolved.

A --set changes the resolved flight configuration for one dispatch, and the dry
run used to echo nothing about it: ``backends.codex.budget_group=null`` gave no
sign that the null took effect, so the only way to confirm an override was to
spend a real dispatch. These cases drive the command end to end with a host
flight layer in a temporary RECKON_HOME, so the values asserted are the ones the
configuration resolution produced rather than a fixture's restatement of them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew

HOST_FLIGHT = """\
default_backend: codex
backends:
  codex:
    launch: cli
    command: codex-fixture
    model: fixture-model
    effort: high
    sandbox: worktree-full
    time_budget: 20m
    budget_group: waves
roles:
  implement:
    backend: codex
    execution_capable: true
    sandbox: worktree-full
    time_budget: 20m
fences:
  time_budget: 20m
  needs_help_after_failures: 2
"""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "target.py"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    (home / "flight.yaml").write_text(HOST_FLIGHT, encoding="utf-8")
    return root


def _arguments(repo: Path, *overrides: str) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "sample",
        "--plan",
        "fixture",
        "--section",
        "guard",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "candidate",
        "--goal",
        "record one dry-run override resolution",
        "--done-when",
        "the command reports each --set path resolved beside its prior value",
        "--write-path",
        "target.py",
        "--session",
        "override-session",
        "--repo",
        str(repo),
        "--dry-run",
        "--no-watch",
    ]
    for override in overrides:
        arguments.extend(["--set", override])
    return arguments


def test_dry_run_reports_each_set_override_resolved_beside_its_prior_value(
    home: Path, repo: Path
) -> None:
    """The done-when case: both --set paths are echoed with before and resolved."""
    result = CliRunner().invoke(
        cli_module.main,
        _arguments(
            repo,
            "backends.codex.budget_group=null",
            "fences.needs_help_after_failures=4",
        ),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["dry_run"] is True
    assert payload["overrides"] == {
        "backends.codex.budget_group": {"before": "waves", "resolved": None},
        "fences.needs_help_after_failures": {"before": 2, "resolved": 4},
    }
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_dry_run_refuses_a_set_path_the_configuration_does_not_know(
    home: Path, repo: Path
) -> None:
    """A misspelled backend name is refused, naming the path, not echoed."""
    result = CliRunner().invoke(
        cli_module.main, _arguments(repo, "backends.ghost.budget_group=null")
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["dry_run"] is True
    assert payload["error"] == "request-error"
    assert "backends.ghost.budget_group" in payload["detail"]
    assert "ghost" in payload["detail"]
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_dry_run_without_overrides_carries_no_resolution_block(
    home: Path, repo: Path
) -> None:
    """No --set means no block: the key appears only when it has something to report."""
    result = CliRunner().invoke(cli_module.main, _arguments(repo))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert "overrides" not in payload
