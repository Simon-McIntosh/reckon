"""The command line hands the repository choice to the library, unnamed.

The library resolves a dispatch repository from the project's registered mount
and refuses a named repository that resolves elsewhere. A command line that
pre-resolves the enclosing repository of the caller defeats both: a dispatch run
from another project's checkout arrives already naming that checkout, so the
mount default is never reached and the worktree is cut from the wrong
repository. Measured 2026-09-19 against two reckon nodes given nova worktrees.

So this pins the command-line dispatch entry point itself: invoked with the
working directory inside a second project's checkout and no ``--repo``, the
worktree must belong to the first project's mount. A named ``--repo`` still
reaches the library unchanged, which the library's own tests cover.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _make_checkout(root: Path, slug: str) -> Path:
    """One git repository carrying a plan and the fleet script it dispatches with."""
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="fixture">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Repository guard</h2></body></html>
""",
        encoding="utf-8",
    )
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
def mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two registered projects, each in a repository of its own."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)

    amber = _make_checkout(tmp_path / "amber", "amber")
    basalt = _make_checkout(tmp_path / "basalt", "basalt")
    (config_home / "mounts.json").write_text(
        json.dumps({"amber": str(amber / "docs"), "basalt": str(basalt / "docs")}),
        encoding="utf-8",
    )
    return {"amber": amber, "basalt": basalt, "home": config_home}


def _arguments() -> list[str]:
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
        "cli-node",
        "--spec-level",
        "guided",
        "--goal",
        "resolve the repository from the project's mount",
        "--done-when",
        "pytest -q tests/test_crew_cli_repository.py reports one passing case",
        "--write-path",
        "src/resolved.txt",
        "--session",
        "cli-repository-session",
        "--no-watch",
    ]


def test_a_command_line_dispatch_without_repo_cuts_from_the_mount(
    mounts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell is not consulted: the working directory is another checkout."""
    monkeypatch.chdir(mounts["basalt"])
    monkeypatch.setattr(
        dispatch_module,
        "_spawn",
        lambda plan, **kwargs: 4242,
    )
    monkeypatch.setattr(
        dispatch_module,
        "watch_state",
        lambda project, *, session=None: {
            "arming_line": f"reckon crew watch --project {project}",
            "attach_line": runs._watch_attach_line(project, session=session),
            "watcher": {},
            "watcher_live": True,
            "session": session,
            "session_attached": None,
            "follower": {},
        },
    )

    result = CliRunner().invoke(cli_module.main, _arguments())
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert Path(payload["repo"]).resolve() == mounts["amber"].resolve()
    assert (
        dispatch_module.repository_identity(payload["worktree"])
        == mounts["amber"].resolve()
    )
