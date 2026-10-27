"""A reload's pruning is housekeeping: it stays off the monitor stream."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
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


def _tree_cache(root: Path) -> Path:
    checkout = follow.checkout_trust
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    cache = checkout._committed_tree_cache() / key
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _filler(cache: Path, name: str, stamp: float) -> Path:
    """A published tree of the given age, as one left by an earlier reload."""
    tree = cache / name
    (tree / "reckon").mkdir(parents=True)
    (tree / "reckon" / "__init__.py").write_text("")
    (tree / ".complete").touch()
    for path in (tree, tree / ".complete"):
        os.utime(path, (stamp, stamp))
    return tree


@pytest.fixture()
def source(tmp_path: Path, monkeypatch):
    # A suite launched from inside a materialised image carries that image's
    # source root and commit; every fixture here builds its own tree, so they
    # are scrubbed rather than left to decide what these checks exercise.
    for carried in (
        follow._FOLLOWER_SOURCE_ROOT_ENV,
        follow._FOLLOWER_IMAGE_COMMIT_ENV,
        "RECKON_FOLLOWER_OWNER",
    ):
        monkeypatch.delenv(carried, raising=False)
    checkout = follow.checkout_trust
    monkeypatch.setattr(
        checkout,
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
    _commit(root, "first")
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
    # The producer's seat: the record the reload's housekeeping lands in.
    handle = (tmp_path / "seat.lock").open("a+b")
    monkeypatch.setitem(runs._WATCH_SEAT_HANDLES, "test-project", handle)
    return root, execs, handle


def test_a_prune_reaches_the_seat_record_and_not_the_stream(
    source, monkeypatch, capsys
):
    """A reload that prunes two trees and keeps one says nothing on the ticker."""
    root, _, handle = source
    cache = _tree_cache(root)
    stamp = time.time() - 3600
    fillers = [
        _filler(cache, f"{index:040x}", stamp + index * 60) for index in range(5)
    ]
    # The oldest was published moments ago, inside the grace before any pin has
    # opened its mark, so the prune beside it keeps that one and takes the two
    # older trees. Its directory stamp stays old, which is what makes it a
    # candidate at all; only its completion marker says it is new.
    os.utime(fillers[0] / ".complete", None)

    reloader = follow._FollowerReloader("test-project", None, seat=True)
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
    _commit(root, "target")
    reloader.poll({})

    out = capsys.readouterr().out
    assert "reload tree" not in out
    prune = runs._read_watch_record(handle)["reload_prune"]
    assert prune["removed"] == 2
    assert str(fillers[0]) in prune["kept"]
    assert prune["when"]


def test_a_removal_that_raises_reaches_the_stream_once(source, monkeypatch, capsys):
    """A prune that fails is the one reload-tree line a reader still needs."""
    root, _, _ = source
    checkout = follow.checkout_trust
    cache = _tree_cache(root)
    stamp = time.time() - 3600
    fillers = {
        _filler(cache, f"{index:040x}", stamp + index * 60) for index in range(4)
    }
    real_rmtree = checkout.shutil.rmtree

    def refuse_a_published_tree(path, *args, **kwargs):
        if Path(path) in fillers:
            raise OSError(f"cannot remove {path}")
        return real_rmtree(path, *args, **kwargs)

    reloader = follow._FollowerReloader("test-project", None, seat=True)
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
    monkeypatch.setattr(checkout.shutil, "rmtree", refuse_a_published_tree)
    _commit(root, "target")
    reloader.poll({})

    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "reload tree" in line or "could not materialize" in line
    ]
    assert len(lines) == 1, lines
    assert any(str(filler) in lines[0] for filler in fillers), lines[0]
