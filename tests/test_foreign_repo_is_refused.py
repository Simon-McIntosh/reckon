"""A dispatch refuses a --repo that belongs to a different project.

A dispatch cuts its worktree from the project's registered mount, so a
``--repo`` naming a repository registered to another project cannot be
honoured. The repository resolution raises for that condition, but on the
picker route it is reached before any handler, so the error surfaced as a
Python traceback instead of the one-line refusal every other invalid argument
produces. The refusal now names the project the repository belongs to beside
the project the dispatch named, so the caller can correct one of them.

The command runs as a real process here, because "a traceback reaches stderr"
is a fact about a process exit and not about a captured exception: an
unhandled error prints a traceback to stderr only when nothing catches it
above the entry point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import pytest

from reckon import crew, crew_dispatch_commands

REPO_ROOT = Path(__file__).resolve().parents[1]

_FLIGHT = """\
version: 1
default_backend: worker
backends:
  worker:
    launch: cli
    command: codex
    model: some-model
    effort: high
    sandbox: worktree-full
    time_budget: 20m
roles:
  implement: {}
fences:
  time_budget: 20m
  needs_help_after_failures: 2
"""

_PLAN = (
    "<!doctype html>\n<html><head>\n"
    '<meta name="docs-project" content="fixture">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="fixture">\n'
    '</head><body><h2 id="guard">Repository guard</h2></body></html>\n'
)


def _make_checkout(root: Path, slug: str) -> Path:
    """One git repository carrying a plan and the fleet script a dispatch uses."""
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        REPO_ROOT / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(_PLAN, encoding="utf-8")
    (root / "seed.txt").write_text(f"{slug}\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture()
def workspace(tmp_path: Path) -> dict:
    """Two registered projects, each its own repository, and a host flight layer."""
    home = tmp_path / "home"
    home.mkdir()
    amber = _make_checkout(tmp_path / "amber", "amber")
    basalt = _make_checkout(tmp_path / "basalt", "basalt")
    (home / "mounts.json").write_text(
        json.dumps({"amber": str(amber / "docs"), "basalt": str(basalt / "docs")}),
        encoding="utf-8",
    )
    flight = tmp_path / "flight.yaml"
    flight.write_text(_FLIGHT, encoding="utf-8")
    return {"home": home, "flight": flight, "amber": amber, "basalt": basalt}


def _arguments(repo: Path) -> list[str]:
    """A picker-routed dispatch naming a repository, which reaches the guard first."""
    return [
        "crew",
        "dispatch",
        "--project",
        "amber",
        "--plan",
        "fixture",
        "--section",
        "guard",
        "--node",
        "foreign-repo-node",
        "--spec-level",
        "guided",
        "--goal",
        "resolve the repository from the project's mount",
        "--done-when",
        "the dispatch is refused naming both projects",
        "--write-path",
        "src/resolved.txt",
        "--session",
        "foreign-repo-session",
        "--no-watch",
        "--route",
        "picker",
        "--repo",
        str(repo),
    ]


def _run_cli(workspace: dict, repo: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Run the command as a real process so its stderr is the process's stderr."""
    env = dict(os.environ)
    env["RECKON_HOME"] = str(workspace["home"])
    env["RECKON_FLIGHT_CONFIG"] = str(workspace["flight"])
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from reckon.cli import main; main()",
            *_arguments(repo),
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_foreign_repository_is_refused_naming_both_projects(
    workspace: dict,
) -> None:
    """--repo naming another project's repository is a one-line refusal, no traceback."""
    result = _run_cli(workspace, workspace["basalt"], cwd=workspace["basalt"])

    assert result.returncode != 0, result.stdout
    assert "Traceback" not in result.stderr, result.stderr
    refusal = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(refusal) == 1, result.stderr
    assert "amber" in refusal[0] and "basalt" in refusal[0], refusal[0]
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "dispatch-refused"


def test_the_mounts_own_repository_is_not_refused(
    workspace: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard admits the project's own repository, so it does not over-refuse."""
    monkeypatch.setenv("RECKON_HOME", str(workspace["home"]))

    crew_dispatch_commands._refuse_a_foreign_repository(
        crew, "amber", workspace["amber"], False
    )

    with pytest.raises(click.exceptions.Exit):
        crew_dispatch_commands._refuse_a_foreign_repository(
            crew, "amber", workspace["basalt"], False
        )
