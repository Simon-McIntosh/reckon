"""A reload uses committed source even while the shared checkout is edited."""

from __future__ import annotations

import ast
import os
import subprocess
from importlib import import_module
from pathlib import Path

import pytest

from reckon import crew_follow_commands as follow
from reckon.crew import runs

_GIT_HOME = os.environ.get("RECKON_HOME")


def _git(root: Path, *args: str) -> str:
    environment = dict(os.environ)
    if _GIT_HOME is None:
        environment.pop("RECKON_HOME", None)
    else:
        environment["RECKON_HOME"] = _GIT_HOME
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _commit(root: Path, value: str) -> str:
    (root / "reckon" / "example.py").write_text(f"VALUE = {value!r}\n")
    _git(root, "add", "reckon/example.py")
    _git(root, "commit", "-m", f"Set example to {value}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def source(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    root = tmp_path / "source"
    (root / "reckon").mkdir(parents=True)
    (root / "reckon" / "__init__.py").write_text("")
    _git(root, "init")
    _git(root, "config", "user.name", "Reload Test")
    _git(root, "config", "user.email", "reload@example.invalid")
    _git(root, "add", "reckon/__init__.py")
    first = _commit(root, "first")
    monkeypatch.setattr(follow, "_import_root", lambda module: root)
    monkeypatch.setattr(
        runs, "follower_code_stamp", lambda: _git(root, "rev-parse", "HEAD")
    )
    monkeypatch.setattr(runs, "FOLLOWER_FRESHNESS_SECONDS", 0)
    monkeypatch.setattr(
        import_module("reckon.crew.dispatch"),
        "_export_launched_workers_for_reexec",
        lambda: None,
    )
    execs = []
    monkeypatch.setattr(follow.os, "execve", lambda *args: execs.append(args))
    return root, first, execs


def _executed_root(execs) -> Path:
    launcher = execs[0][1][2]
    literal = launcher.split("sys.path.insert(0, ", 1)[1].split(");", 1)[0]
    return Path(ast.literal_eval(literal))


def test_dirty_checkout_reloads_the_committed_tree(source, capsys):
    root, first, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    second = _commit(root, "second")
    (root / "reckon" / "example.py").write_text("VALUE = 'uncommitted'\n")

    reloader.poll({})

    assert len(execs) == 1, capsys.readouterr().out
    imported = _executed_root(execs)
    assert imported != root
    assert (imported / "reckon" / "example.py").read_text() == "VALUE = 'second'\n"
    assert "uncommitted" not in (imported / "reckon" / "example.py").read_text()
    assert second in str(imported)
    assert execs[0][2][follow._FOLLOWER_SOURCE_ROOT_ENV] == str(root)
    assert execs[0][2][follow._FOLLOWER_IMAGE_COMMIT_ENV] == second


def test_materialized_image_tracks_later_source_commit(source, monkeypatch):
    root, first, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    second = _commit(root, "second")
    reloader.poll({})
    assert len(execs) == 1
    second_root = _executed_root(execs)
    carried = execs[0][2]
    monkeypatch.setenv(
        follow._FOLLOWER_SOURCE_ROOT_ENV, carried[follow._FOLLOWER_SOURCE_ROOT_ENV]
    )
    monkeypatch.setenv(
        follow._FOLLOWER_IMAGE_COMMIT_ENV, carried[follow._FOLLOWER_IMAGE_COMMIT_ENV]
    )
    monkeypatch.setattr(follow, "_import_root", lambda module: second_root)
    replacement = follow._FollowerReloader("test-project", None)
    assert replacement.import_root == second_root
    assert replacement.source_root == root
    assert replacement.code_stamp == second

    clock = [100.0]
    monkeypatch.setattr(follow.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runs, "FOLLOWER_FRESHNESS_SECONDS", 1.0)
    replacement.poll({})
    third = _commit(root, "third")
    clock[0] += 2.0
    replacement.poll({})

    assert len(execs) == 2
    third_root = _executed_root([execs[1]])
    assert third_root != second_root
    assert third in str(third_root)
    assert (third_root / "reckon" / "example.py").read_text() == "VALUE = 'third'\n"


def test_unimportable_committed_module_defers_and_names_it(source, capsys):
    root, first, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    (root / "reckon" / "example.py").write_text("import absent_reload_dependency\n")
    _git(root, "add", "reckon/example.py")
    _git(root, "commit", "-m", "Break example import")

    reloader.poll({})

    line = capsys.readouterr().out
    assert "does not import" in line
    assert "absent_reload_dependency" in line
    assert execs == []


def test_archive_timeout_keeps_the_running_image(source, monkeypatch, capsys):
    root, first, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    _commit(root, "second")
    run = subprocess.run

    def timed_archive(args, **kwargs):
        if "archive" in args:
            raise subprocess.TimeoutExpired(args, 1)
        return run(args, **kwargs)

    monkeypatch.setattr(follow.checkout_trust.subprocess, "run", timed_archive)
    reloader.poll({})

    line = capsys.readouterr().out
    assert "could not materialize HEAD" in line
    assert "timed out" in line
    assert execs == []


def test_old_materialized_trees_are_removed_and_named(source, monkeypatch, capsys):
    root, first, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == first
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
    imported = []

    for index in range(follow.checkout_trust.COMMITTED_TREE_RETENTION + 1):
        commit = _commit(root, f"later-{index}")
        reloader.poll({})
        assert len(execs) == index + 1
        imported.append(_executed_root([execs[index]]))
        assert (imported[-1] / "reckon" / "example.py").read_text() == (
            f"VALUE = 'later-{index}'\n"
        )
        reloader.code_stamp = commit

    line = capsys.readouterr().out
    assert not imported[0].exists()
    assert all(path.exists() for path in imported[1:])
    assert f"removed old reload tree {imported[0]}" in line
