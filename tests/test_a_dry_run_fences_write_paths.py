"""A dry run applies the write-path fence the real dispatch applies.

The dry run's documented job is to report the verdict the real dispatch reaches,
so a validating caller must not be told a dispatch would proceed that the launch
refuses. The real path resolves the project's registered mount and hands it to
``plan_dispatch``, where ``_require_write_paths_in_authority`` refuses a write
path outside the repository and the delivery roots. The preview passes the
caller's ``--repo`` through unchanged, so with no ``--repo`` the authority block
is skipped entirely and every write path passes the preview.

These cases drive the command end to end through click's runner. The outside
path is a real ``~/.cache`` of a temporary home, which no delivery root covers,
and the same dispatch is refused by the real path — so the equality asserted is
between two refusals of one call, not between a refusal and a guess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew import runs

# The cases decide on the watcher and the lane, so arming is allowed for the
# module even though nothing here arms a producer: the gate reads watcher state
# and the registration they write is a running process of their own.
pytestmark = pytest.mark.arms_watch_producer

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configuration home the follower registry and pointer dir resolve into."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    """A mountable repository carrying the plan and the fleet script dispatch needs."""
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
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    for arguments in (
        ["add", "seed.txt", "skills", "docs"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *args, **kwargs: CONFIG
    )


def _spawn_runner() -> subprocess.Popen[str]:
    """A long-lived process used as a genuinely running registered watcher."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _register_watcher(project: str, pid: int) -> None:
    """Register a running process as the project's watcher seat."""
    path = crew.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        crew._write_watch_record(
            handle,
            {"pid": pid, "pid_start_time": crew._process_start_time(pid)},
        )


def _release_follower(project: str, session: str) -> None:
    """Arm a follower and let it release, leaving the file a release leaves."""
    with runs.follower_registration(project, session, delivery="stream"):
        pass


def _arguments(
    node: str, write_path: str, session: str, *, repo: Path | None, dry_run: bool
) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "sample",
        "--plan",
        "fixture",
        "--section",
        "guard",
        "--spec-level",
        "exact",
        "--node",
        node,
        "--goal",
        "record dispatch admission for one session",
        "--done-when",
        "the command reports the watcher admission judgement",
        "--write-path",
        write_path,
        "--session",
        session,
    ]
    if repo is not None:
        arguments += ["--repo", str(repo)]
    if dry_run:
        arguments.append("--dry-run")
    return arguments


def _invoke(arguments: list[str], runner: subprocess.Popen[str], project: str, session: str):
    _register_watcher(project, runner.pid)
    _release_follower(project, session)
    return CliRunner().invoke(cli_module.main, arguments)


def _payload(result) -> dict:
    """The JSON document a refusal emits, before the plain-text stderr line."""
    return json.loads(result.output.splitlines()[0])


def test_a_dry_run_without_repo_refuses_a_write_path_outside_the_repository(
    home: Path, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview reaches the same refusal, in the same words, as the launch.

    With no ``--repo`` the preview used to pass ``repo=None`` and skip the
    authority block that holds the fence, so the outside path was accepted; the
    launch resolves the project's mount and refuses it. Both arms run here so
    the case measures the preview against the real call rather than against a
    guess, and the comparison is on the refusal sentence both produced.
    """
    temporary_home = tmp_path / "operator-home"
    temporary_home.mkdir()
    monkeypatch.setenv("HOME", str(temporary_home))
    outside = temporary_home / ".cache" / "unclaimed" / "artifact.json"

    project = "sample"
    preview_session = "session-preview"
    launch_session = "session-launch"
    runner = _spawn_runner()
    try:
        preview = _invoke(
            _arguments(
                "outside-preview",
                str(outside),
                preview_session,
                repo=None,
                dry_run=True,
            ),
            runner,
            project,
            preview_session,
        )
        launch = _invoke(
            _arguments(
                "outside-launch",
                str(outside),
                launch_session,
                repo=repo,
                dry_run=False,
            ),
            runner,
            project,
            launch_session,
        )

        assert preview.exit_code == 1, preview.output
        assert launch.exit_code == 1, launch.output
        preview_refusal = _payload(preview)["detail"]
        launch_refusal = _payload(launch)["detail"]
        assert "outside the authorised work repository" in preview_refusal
        assert str(outside) in preview_refusal
        assert preview_refusal == launch_refusal
        assert not list(crew.list_live(project=project)), "nothing may be created"
    finally:
        runner.terminate()
        runner.wait(timeout=5)


def test_a_dry_run_accepts_a_write_path_inside_the_repository(
    home: Path, repo: Path
) -> None:
    """A path inside the repository is not refused by the preview."""
    project = "sample"
    session = "session-inside"
    runner = _spawn_runner()
    try:
        preview = _invoke(
            _arguments(
                "inside-preview",
                "src/candidate.py",
                session,
                repo=repo,
                dry_run=True,
            ),
            runner,
            project,
            session,
        )

        assert preview.exit_code == 0, preview.output
        payload = json.loads(preview.output)
        assert payload["ok"] is True
        assert payload["dry_run"] is True
    finally:
        runner.terminate()
        runner.wait(timeout=5)