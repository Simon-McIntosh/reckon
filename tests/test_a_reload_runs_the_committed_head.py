"""A reload uses committed source even while the shared checkout is edited."""

from __future__ import annotations

import ast
import fcntl
import os
import select
import shutil
import subprocess
import sys
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
    monkeypatch.setattr(
        follow.checkout_trust,
        "_committed_tree_cache",
        lambda: tmp_path / "home" / "crew" / "reload-trees",
    )
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


@pytest.fixture()
def seat(tmp_path: Path, monkeypatch):
    """The producer's seat, where a reload's housekeeping is recorded.

    A reload prunes its image cache as housekeeping, and the fact worth keeping
    is the last prune rather than one ticker line per tree per reload. The
    seat's record is where that lands, so a reload driven here holds one.
    """
    handle = (tmp_path / "seat.lock").open("a+b")
    monkeypatch.setitem(runs._WATCH_SEAT_HANDLES, "test-project", handle)
    yield handle
    handle.close()


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


def test_old_materialized_trees_are_removed_quietly(source, monkeypatch, capsys, seat):
    root, first, execs = source
    reloader = follow._FollowerReloader("test-project", None, seat=True)
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
    # The oldest image is gone from the cache and named in no ticker line, which
    # is the housekeeping a reader is not meant to be relayed; the seat's record
    # is where the count of what the prune removed is kept.
    assert "reload tree" not in line
    prune = runs._read_watch_record(seat)["reload_prune"]
    assert prune["removed"] == 1
    assert str(imported[0]) not in prune["kept"]


def test_running_image_survives_cache_pruning(source, monkeypatch, capsys, seat):
    root, first, execs = source
    children = []

    def capture_exec(*args):
        execs.append(args)
        if children:
            return
        descriptor = int(args[2][follow._FOLLOWER_IMAGE_PIN_ENV])
        assert os.get_inheritable(descriptor)
        environment = dict(args[2])
        environment["RECKON_TEST_IMAGE_ROOT"] = str(_executed_root([args]))
        child = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import os, sys; fd = int(os.environ['RECKON_FOLLOWER_IMAGE_PIN']); "
                    "os.fstat(fd); print('READY', flush=True); sys.stdin.readline(); "
                    "sys.path.insert(0, os.environ['RECKON_TEST_IMAGE_ROOT']); "
                    "from reckon import example; print(example.VALUE, flush=True)"
                ),
            ],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=False,
        )
        children.append(child)
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == "READY"

    monkeypatch.setattr(follow.os, "execve", capture_exec)
    reloader = follow._FollowerReloader("test-project", None, seat=True)
    assert reloader.code_stamp == first
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
    first_image_commit = _commit(root, "running")
    reloader.poll({})
    first_image = _executed_root(execs)
    assert (first_image / "reckon" / "example.py").read_text() == (
        "VALUE = 'running'\n"
    )
    child = children[0]
    try:
        reloader.code_stamp = first_image_commit
        for index in range(follow.checkout_trust.COMMITTED_TREE_RETENTION + 1):
            commit = _commit(root, f"newer-{index}")
            reloader.poll({})
            reloader.code_stamp = commit
        assert first_image.is_dir()
        # The image a live child is importing from is kept by the prune, and
        # the seat's record is where the paths a lease kept are read.
        assert "reload tree" not in capsys.readouterr().out
        prune = runs._read_watch_record(seat)["reload_prune"]
        assert str(first_image) in prune["kept"]
        child.stdin.write("\n")
        child.stdin.flush()
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == "running"
        assert child.wait(timeout=5) == 0, child.stderr.read()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)

    commit = _commit(root, "after-release")
    reloader.poll({})
    reloader.code_stamp = commit
    assert not first_image.exists()
    # Once nothing holds it, the image goes and stops being kept; the ticker
    # carries no line for either half.
    assert "reload tree" not in capsys.readouterr().out
    after = runs._read_watch_record(seat)["reload_prune"]
    assert str(first_image) not in after["kept"]
    assert after["removed"] >= 1


def test_a_pruner_between_open_and_lock_never_leaves_a_plain_file(source, monkeypatch):
    root, _, _ = source
    commit = _commit(root, "second")
    checkout = follow.checkout_trust
    image, _, _, pin = checkout.materialize_committed_tree(root, commit)
    os.close(pin)
    lock = checkout.fcntl.flock
    raced = False

    def prune_before_shared_lock(descriptor, operation):
        nonlocal raced
        if operation == fcntl.LOCK_SH and not raced:
            raced = True
            pruning = os.open(image / ".in-use", os.O_RDWR)
            try:
                lock(pruning, fcntl.LOCK_EX | fcntl.LOCK_NB)
                shutil.rmtree(image)
            finally:
                os.close(pruning)
        return lock(descriptor, operation)

    monkeypatch.setattr(checkout.fcntl, "flock", prune_before_shared_lock)
    with pytest.raises(RuntimeError, match="vanished"):
        checkout.materialize_committed_tree(root, commit)
    assert not image.exists()
    image, _, _, pin = checkout.materialize_committed_tree(root, commit)
    try:
        assert image.is_dir()
        assert (image / "reckon" / "example.py").read_text() == "VALUE = 'second'\n"
    finally:
        os.close(pin)


def test_a_materialized_image_renews_its_lease_on_each_poll(source, monkeypatch):
    root, _, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    commit = _commit(root, "second")
    reloader.poll({})
    image = _executed_root(execs)
    monkeypatch.setenv(follow._FOLLOWER_SOURCE_ROOT_ENV, str(root))
    monkeypatch.setenv(follow._FOLLOWER_IMAGE_COMMIT_ENV, commit)
    monkeypatch.setattr(follow, "_import_root", lambda module: image)
    running = follow._FollowerReloader("test-project", None)
    try:
        lease = image / ".in-use"
        old = lease.stat().st_mtime_ns - 10_000_000_000
        os.utime(lease, ns=(old, old))
        running.checked_at = follow.time.monotonic()
        monkeypatch.setattr(runs, "FOLLOWER_FRESHNESS_SECONDS", 60)
        running.poll({})
        assert lease.stat().st_mtime_ns > old
    finally:
        os.close(running.image_pin)


def test_pruning_keeps_its_own_import_root(source):
    root, _, _ = source
    checkout = follow.checkout_trust
    running_commit = _commit(root, "running")
    image, _, _, pin = checkout.materialize_committed_tree(root, running_commit)
    os.close(pin)

    for index in range(checkout.COMMITTED_TREE_RETENTION + 1):
        commit = _commit(root, f"later-{index}")
        _, _, kept, pin = checkout.materialize_committed_tree(
            root, commit, active_root=image
        )
        os.close(pin)

    assert image.is_dir()
    assert image in kept
    commit = _commit(root, "after-release")
    _, removed, _, pin = checkout.materialize_committed_tree(root, commit)
    os.close(pin)
    assert image in removed
    assert not image.exists()
