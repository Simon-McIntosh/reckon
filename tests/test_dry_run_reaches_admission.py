"""A dispatch dry run reaches the watcher admission judgement through the command.

The dry run's documented job is to report the verdict a real dispatch would
reach, so ``reckon crew dispatch --dry-run`` must consult the watcher gate the
launching path consults. When the command's dry-run branch called
``plan_dispatch`` with no session and no ``watch_required``, the gate was never
evaluated and a validating caller reached no admission judgement at all: a
session with no follower to release reported a dispatchable node that the real
dispatch then refused.

These cases drive the command end to end through click's runner, and assert the
same two verdicts the launching path reaches: a released registration proceeds
with the re-arm warning in the output, and a session that never registered is
refused as ``watcher-required``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import runs

# These cases decide on the watcher, so arming is allowed for the module even
# though nothing here arms a producer: the gate reads watcher state and the
# registration they write is a running process of their own.
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
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)


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


def _arguments(repo: Path, session: str) -> list[str]:
    return [
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
        "candidate",
        "--goal",
        "record dispatch admission for one session",
        "--done-when",
        "the command reports the watcher admission judgement",
        "--write-path",
        "src/candidate.py",
        "--session",
        session,
        "--repo",
        str(repo),
        "--dry-run",
    ]


def test_dry_run_reports_the_released_follower_proceeding_with_the_rearm_warning(
    home: Path, repo: Path
) -> None:
    """A released registration proceeds, and the output carries the re-arm line."""
    project = "sample"
    session = "session-released"
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        _release_follower(project, session)

        result = CliRunner().invoke(cli_module.main, _arguments(repo, session))

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["dry_run"] is True
        attach_line = runs.watch_state(project, session=session)["attach_line"]
        warnings = [str(item) for item in payload["warnings"]]
        assert any(attach_line in item for item in warnings), warnings
    finally:
        runner.terminate()
        runner.wait(timeout=5)


def test_dry_run_refuses_a_never_registered_follower_as_watcher_required(
    home: Path, repo: Path
) -> None:
    """A session that never armed a follower is refused by the command itself."""
    project = "sample"
    session = "session-never"
    runner = _spawn_runner()
    try:
        _register_watcher(project, runner.pid)
        # No release, no registration file: this session never armed one.
        assert not runs.follower_lock_path(project, session).is_file()

        result = CliRunner().invoke(cli_module.main, _arguments(repo, session))

        assert result.exit_code == 8, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"] == "watcher-required"
        assert session in payload["detail"]
        assert not list(crew.list_live(project=project)), "nothing may be created"
    finally:
        runner.terminate()
        runner.wait(timeout=5)
