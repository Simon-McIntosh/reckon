"""A watcher a test session arms is terminated when that session ends.

Dispatch arms a project watcher detached on purpose, so it survives the process
that asked for it. Under a test that is a leak: the test ends, its temporary
configuration home is abandoned, and the producer keeps polling a directory
nothing will write to again. The suite's session fixture answers for it, and
this module measures that it does — by running the arming half of the dispatch
path in a child pytest, letting that session end, and requiring that no watcher
naming the child's home is still running a second later.

The child runs the arming half only. The dispatch test in ``tests/test_crew.py``
unwatches at its own end, so running it whole would unwind the very producer
this measures and the measure would pass whether the session fixture worked or
not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import runs
from reckon.crew.dispatch import WATCH_ARMING_ENV
from tests.conftest import reapable_watch_pids
from tests.test_crew import CONFIG, _write_running_pointer, home, repo  # noqa: F401

WORKTREE = Path(__file__).resolve().parents[1]
CHILD_MODULE = "tests/test_test_watchers_are_reaped.py"
ARM_ENV = "RECKON_TEST_REAP_ARM"


# ── reading what the child sessions left behind ─────────────────────────────


def watch_processes_naming(root: Path) -> list[tuple[int, str]]:
    """Live ``crew watch`` processes whose environment names a home under ``root``.

    The environment decides, not the command line: a producer's home is part of
    what it was started with, so a process naming this run's temporary root is
    one this run started and may end.
    """
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"watch" not in argv or b"--project" not in argv:
            continue
        for item in environ:
            name, _, value = item.partition(b"=")
            if name != b"RECKON_HOME" or not value:
                continue
            named = Path(os.fsdecode(value))
            if named == root or root in named.parents:
                found.append((int(entry.name), os.fsdecode(value)))
    return found


def armed_projects(root: Path) -> list[str]:
    """Projects whose seat records exist under ``root``, whatever their liveness."""
    projects: list[str] = []
    for record in root.rglob("*.lock"):
        if record.parent.name != "watch":
            continue
        try:
            value = json.loads(record.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("project"):
            projects.append(str(value["project"]))
    return projects


# ── the child that arms and does not clean up after itself ──────────────────


@pytest.mark.arms_watch_producer
@pytest.mark.skipif(
    os.environ.get(ARM_ENV) != "1",
    reason="runs only in the child session the parent spawns",
)
def test_arms_a_watcher_without_reaping_it(
    home,  # noqa: F811 - re-exported from tests.test_crew for pytest
    repo,  # noqa: F811 - re-exported from tests.test_crew for pytest
    monkeypatch,
) -> None:
    """The arming half of the dispatch path, stopping before its own unwatch.

    Same fixtures, same patches and same command as
    ``tests/test_crew.py::test_cli_dispatch_arms_a_missing_watcher``; the only
    difference is that it ends while the producer is still live, which is the
    state a session fixture has to answer for.
    """
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: CONFIG)
    monkeypatch.setattr(crew, "_spawn", lambda *args, **kwargs: 4242)
    _write_running_pointer(
        home,
        "r-existing",
        repo=str(repo),
        write_paths=["reckon/_backends.py"],
    )

    with runs.follower_claim("proj", "sess", delivery="stream"):
        result = CliRunner().invoke(
            cli_module.main,
            [
                "crew",
                "dispatch",
                "--project",
                "proj",
                "--plan",
                "plan-a",
                "--section",
                "s3",
                "--spec-level",
                "guided",
                "--node",
                "node-a",
                "--goal",
                "record the launch matrix for one backend",
                "--done-when",
                "uv run pytest tests/test_crew.py reports 0 failures",
                "--write-path",
                "reckon/crew.py",
                "--session",
                "sess",
                "--repo",
                str(repo),
            ],
        )

    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert payload["watch"]["watcher_live"] is True
    assert payload["watch"]["arming_line"] == "reckon crew watch --project proj"


# ── the measure ─────────────────────────────────────────────────────────────


def test_a_watcher_armed_by_a_child_session_is_not_left_running(
    tmp_path: Path,
) -> None:
    child_base = tmp_path / "child-base"
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {WATCH_ARMING_ENV, "RECKON_HOME", ARM_ENV}
    }
    child_env[ARM_ENV] = "1"
    child_env["PYTHONPATH"] = str(WORKTREE)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--basetemp",
            str(child_base),
            f"{CHILD_MODULE}::test_arms_a_watcher_without_reaping_it",
        ],
        cwd=WORKTREE,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    # The measure only means something if the child armed a producer at all: a
    # child that never reached the arming path would leave nothing to reap and
    # read exactly like a pass.
    assert "proj" in armed_projects(child_base), (
        "the child session left no seat record for 'proj', so it never armed a "
        "watcher and the reap below measured nothing"
    )

    time.sleep(1.0)
    survivors = watch_processes_naming(child_base)
    assert survivors == [], (
        f"a watch producer outlived the child pytest session: {survivors}"
    )


# ── the teardown refuses what it did not start ──────────────────────────────


def test_a_record_naming_another_home_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "own-home" / "crew" / "watch").mkdir(parents=True)
    (root / "stale-home" / "crew" / "watch").mkdir(parents=True)

    def spawn(config_home: Path) -> subprocess.Popen:
        environment = dict(os.environ)
        environment["RECKON_HOME"] = str(config_home)
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=environment,
        )

    ours = spawn(root / "own-home")
    foreign = spawn(tmp_path / "elsewhere")
    try:
        (root / "own-home" / "crew" / "watch" / "proj-00.lock").write_text(
            json.dumps({"pid": ours.pid, "project": "proj"})
        )
        # A record under this run's root whose process now names a different
        # home: the pid was reused, so signalling it would reach a separate run.
        (root / "stale-home" / "crew" / "watch" / "proj-11.lock").write_text(
            json.dumps({"pid": foreign.pid, "project": "proj"})
        )

        reapable = reapable_watch_pids(root)
        assert ours.pid in reapable
        assert foreign.pid not in reapable
    finally:
        ours.terminate()
        foreign.terminate()
        ours.wait()
        foreign.wait()
