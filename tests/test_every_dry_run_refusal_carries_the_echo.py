"""Every dry-run refusal document carries dry_run and the --set echo.

Two refusals used to answer the dispatch preview with a shape of their own:
the availability competence refusal (exit 5) was emitted before the --set
resolution had been computed, and the --local backend-selection failure
answered without the dry_run marker and without the echo at all. A caller
reading either refusal therefore could not tell a --set override that resolved
from one that never applied — the reading the dry run exists to give.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands

dispatch_module = importlib.import_module("reckon.crew.dispatch")

DONE_WHEN = "the refusal reports each --set path resolved beside its prior value"

OVERRIDE_PATH = "backends.codex.budget_group"
OVERRIDE_ECHO = {OVERRIDE_PATH: {"before": "waves", "resolved": None}}

CATALOG_SCRIPT = "#!/bin/sh\nprintf '%s\\n' 'served-alpha gpu-a' 'served-beta gpu-b'\n"

UNAVAILABLE_MODEL = "unserved-gamma"


def _host_flight(*, local_backend: str | None = None) -> str:
    """The host layer: one cli backend whose catalog omits its configured model."""
    local_line = f"local_backend: {local_backend}\n" if local_backend else ""
    return (
        "default_backend: codex\n"
        f"{local_line}"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: synthetic-worker\n"
        f"    model: {UNAVAILABLE_MODEL}\n"
        "    effort: high\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "    budget_group: waves\n"
        "    catalog:\n"
        '      list_command: ["synthetic-worker", "--list"]\n'
        "      model_pattern: '^{model}\\b'\n"
        "roles:\n"
        "  implement:\n"
        "    backend: codex\n"
        "    execution_capable: true\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "fences:\n"
        "  time_budget: 20m\n"
        "  needs_help_after_failures: 2\n"
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose backend command is a synthetic catalog on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    worker = bin_dir / "synthetic-worker"
    worker.write_text(CATALOG_SCRIPT, encoding="utf-8")
    worker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
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
    return root


def _arguments(repo: Path, *, local: bool = False) -> list[str]:
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
        "record one dry-run refusal echo",
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "target.py",
        "--session",
        "refusal-echo-session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
        "--set",
        f"{OVERRIDE_PATH}=null",
    ]
    if local:
        arguments.append("--local")
    return arguments


def test_a_dry_run_availability_refusal_carries_the_echo(
    home: Path, repo: Path
) -> None:
    """The exit-5 competence refusal is a dry-run document like the rest."""
    (home / "flight.yaml").write_text(_host_flight(), encoding="utf-8")

    result = CliRunner().invoke(cli_module.main, _arguments(repo))

    assert result.exit_code == 5, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "competence-refusal"
    assert payload["competence"]["refusal"] == "model-unavailable"
    assert payload["dry_run"] is True
    assert payload["overrides"] == OVERRIDE_ECHO
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_a_dry_run_local_failure_carries_the_echo(home: Path, repo: Path) -> None:
    """The --local selection failure answers on the same dry-run channel."""
    (home / "flight.yaml").write_text(_host_flight(), encoding="utf-8")

    result = CliRunner().invoke(cli_module.main, _arguments(repo, local=True))

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "request-error"
    assert "local_backend" in payload["detail"]
    assert "must be set" in payload["detail"], "the refusal names what is missing"
    assert payload["dry_run"] is True
    assert payload["overrides"] == OVERRIDE_ECHO
    assert not list(crew.list_live(project="sample")), "nothing may be created"


def test_a_dry_run_budget_hold_carries_the_echo(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held wave is a dry-run refusal document like the rest."""
    (home / "flight.yaml").write_text(_host_flight(), encoding="utf-8")
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        dispatch_module,
        "dispatch_picker_selection",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        dispatch_module, "resolve_project_repository", lambda *_a, **_k: repo
    )

    def held(**_kwargs):
        raise crew.BudgetHold({"backend": "codex", "reason": "the window is spent"})

    monkeypatch.setattr(crew, "plan_dispatch", held)

    result = CliRunner().invoke(cli_module.main, _arguments(repo))

    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "budget-hold"
    assert payload["dry_run"] is True
    assert payload["overrides"] == OVERRIDE_ECHO
    assert not list(crew.list_live(project="sample")), "nothing may be created"
