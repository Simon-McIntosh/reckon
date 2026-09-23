"""A test never leaves a watch producer behind it.

Arming is detached by design, so a producer started under a test outlives it
and polls a configuration home that is about to be deleted. Two independent
mechanisms are asserted here, because a convention alone does not hold: the
shared fixture suppresses arming so a real producer is opted into, and the
arming path itself refuses a configuration home under a throwaway test
directory even when a caller bypasses the fixture.
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.dispatch import WATCH_ARMING_ENV

# `reckon.crew` re-exports the dispatch callable under the submodule's name, so
# the module itself is reached through the import machinery.
dispatch_module = importlib.import_module("reckon.crew.dispatch")


CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def fixture_project(isolated_reckon_home: Path, tmp_path: Path, monkeypatch):
    """A dispatchable project whose configuration home is the shared temp one."""
    config_home = isolated_reckon_home
    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="arming">Producer arming</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record one dispatch that arms nothing",
        plan="fixture",
        section="arming",
        spec_level="guided",
        done_when="pytest reports no producer left behind by the suite",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _dispatch(config_home: Path, repo: Path, name: str, **kwargs) -> dict:
    return crew.dispatch(
        node=_node(config_home, name),
        project="sample",
        repo=repo,
        config=CONFIG,
        session=kwargs.pop("session", f"session-{name}"),
        launcher=lambda *args, **kwargs: os.getpid(),
        watch_required=True,
        **kwargs,
    )


def _producer_processes(project: str) -> list[str]:
    """Every live process whose argv is a watch producer for ``project``."""
    listing = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=True
    ).stdout
    marker = f"crew watch --project {project}"
    return [line.strip() for line in listing.splitlines() if marker in line]


def _assert_attach_line_shape(
    line: str, project: str, session: str | None = None
) -> None:
    """Assert the attach line's shape, never a literal or the composer itself.

    The first token has to be an absolute path to the running ``reckon``
    console script, because the shell that arms the line need not carry the
    interpreter's bin directory on PATH. The remaining tokens are the fixed
    command carrying exactly the caller's project and session.
    """
    tokens = shlex.split(line)
    executable = tokens[0] if tokens else ""
    assert os.path.isabs(executable), f"the first token is not absolute: {line!r}"
    assert os.path.isfile(executable), f"the first token is not a file: {line!r}"
    assert os.access(executable, os.X_OK), f"the first token is not runnable: {line!r}"
    assert os.path.basename(executable) == "reckon", (
        f"the first token is not the reckon console script: {line!r}"
    )
    expected = ["crew", "follow", "--project", project]
    if session is not None:
        expected += ["--session", session]
    assert tokens[1:] == expected, (
        f"the attach line's arguments are not the fixed command: {line!r}"
    )


def test_a_dispatch_under_the_shared_fixture_arms_no_producer(
    fixture_project: tuple[Path, Path],
) -> None:
    config_home, repo = fixture_project
    before = _producer_processes("sample")

    record = _dispatch(config_home, repo, "suppressed")

    assert os.environ[WATCH_ARMING_ENV] == "off"
    assert record["watch"]["watcher_live"] is False
    assert record["watch_override"]["requested"] is True
    assert crew.watch_state("sample")["watcher_live"] is False
    assert _producer_processes("sample") == before


def test_the_suppressed_dispatch_records_the_same_waiver_as_an_explicit_one(
    fixture_project: tuple[Path, Path],
) -> None:
    """The no-watch waiver's shape is the seam, so it must not have moved."""
    config_home, repo = fixture_project

    def assert_waiver_shape(override: dict, session: str) -> None:
        assert override["requested"] is True
        assert override["arming_line"] == "reckon crew watch --project sample"
        assert override["watcher_live"] is False
        assert override["session_attached"] is False
        _assert_attach_line_shape(override["attach_line"], "sample", session)

    suppressed = _dispatch(config_home, repo, "implicit", session="implied")
    explicit = _dispatch(
        config_home, repo, "explicit", session="asked", watch_override=True
    )

    assert_waiver_shape(explicit["watch_override"], "asked")
    assert_waiver_shape(suppressed["watch_override"], "implied")
    assert_waiver_shape(
        crew.read_pointer(explicit["run_id"])["watch_override"], "asked"
    )
    assert_waiver_shape(
        crew.read_pointer(suppressed["run_id"])["watch_override"], "implied"
    )


def test_arming_refuses_a_configuration_home_under_a_test_directory(
    isolated_reckon_home: Path, monkeypatch
) -> None:
    """A caller that bypasses the fixture still cannot spawn a producer."""
    monkeypatch.delenv(WATCH_ARMING_ENV, raising=False)
    before = _producer_processes("sample")

    with pytest.raises(crew.CrewError) as refusal:
        dispatch_module._ensure_watch_producer("sample", session="bypassing")

    message = str(refusal.value)
    assert str(isolated_reckon_home) in message
    assert "outlive" in message
    assert WATCH_ARMING_ENV in message
    assert _producer_processes("sample") == before


def test_arming_proceeds_for_an_ordinary_configuration_home(monkeypatch) -> None:
    """The refusal is bound to the throwaway home, not to arming itself."""
    ordinary = Path(tempfile.mkdtemp(prefix="reckon-ordinary-home-"))
    launched: list[list[str]] = []

    class _Supervisor:
        def poll(self) -> None:
            return None

    def record_spawn(argv, **_kwargs):
        launched.append([str(item) for item in argv])
        return _Supervisor()

    try:
        monkeypatch.setenv("RECKON_HOME", str(ordinary))
        monkeypatch.delenv(WATCH_ARMING_ENV, raising=False)
        monkeypatch.setattr(dispatch_module.subprocess, "Popen", record_spawn)
        monkeypatch.setattr(dispatch_module, "_watch_executable", lambda: "reckon")

        dispatch_module._start_watch_producer("sample")
    finally:
        shutil.rmtree(ordinary, ignore_errors=True)

    assert len(launched) == 1
    assert launched[0][-4:] == ["crew", "watch", "--project", "sample"]


def test_an_opted_in_caller_is_not_refused(
    isolated_reckon_home: Path, monkeypatch
) -> None:
    """A test that reaps its own producer opts in and the guard stands down."""
    monkeypatch.setenv(WATCH_ARMING_ENV, "on")
    dispatch_module._refuse_arming_under_a_throwaway_home("sample")

    monkeypatch.setenv(WATCH_ARMING_ENV, "off")
    with pytest.raises(crew.CrewError):
        dispatch_module._refuse_arming_under_a_throwaway_home("sample")


def test_the_shared_fixture_keeps_writes_out_of_the_real_home(
    fixture_project: tuple[Path, Path],
) -> None:
    """An isolated read does not prove an isolated write, so assert the write.

    Two watch locks carrying test input names were found in the real home,
    which is what a resolved home nobody checked looks like afterwards.
    """
    config_home, repo = fixture_project
    store = importlib.import_module("reckon._store")
    real_home = Path.home() / ".config" / "reckon"
    before = sorted(path.name for path in (real_home / "crew" / "watch").glob("*"))

    record = _dispatch(config_home, repo, "isolated")

    assert store._config_home() == config_home
    assert Path(record["manifest_path"]).is_relative_to(config_home)
    after = sorted(path.name for path in (real_home / "crew" / "watch").glob("*"))
    assert after == before


def test_a_producer_is_bound_to_this_run_by_the_home_it_reports_into(
    tmp_path: Path,
) -> None:
    """The record's home is the binding, not the process name.

    A producer armed by an opted-in test is detached and is nobody's child, so
    no process tree ties it to this run. What does is the seat record it writes
    into its own configuration home's ``crew/watch`` directory: the record names
    the pid, and the directory's location names the run. A record under another
    run's home belongs to that run and must not be found from this one.
    """
    from tests.conftest import watcher_record_pids

    inside = tmp_path / "inside"
    outside = Path(tempfile.mkdtemp(prefix="reckon-ordinary-home-"))
    (inside / "crew" / "watch").mkdir(parents=True)
    (outside / "crew" / "watch").mkdir(parents=True)

    def register(home: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            env={**os.environ, "RECKON_HOME": str(home)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (home / "crew" / "watch" / "sample-00.lock").write_text(
            json.dumps({"pid": process.pid, "project": "sample"}),
            encoding="utf-8",
        )
        return process

    processes = [register(inside), register(outside)]
    try:
        found = watcher_record_pids(tmp_path)

        assert found == [(processes[0].pid, inside)]
        assert not watcher_record_pids(Path("/nonexistent-root"))
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=10)
        shutil.rmtree(outside, ignore_errors=True)
