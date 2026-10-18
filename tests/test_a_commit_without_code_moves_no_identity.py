"""A code identity names the reckon/ package content, not the commit at HEAD.

A commit that changes no file under ``reckon/`` -- a plan edit, a run-state
write, a promotion record -- leaves the identity unchanged, so it reloads no
follower or producer and extracts no reload tree. Two commits with the same
package read alike however many commits sit between them.
"""

from __future__ import annotations

import ast
import os
import subprocess
from importlib import import_module
from pathlib import Path

import pytest

from reckon import checkout_trust
from reckon import crew_follow_commands as follow
from reckon.crew import runs

_GIT_HOME = os.environ.get("RECKON_HOME")
_IMAGE_COMMIT_ENV = "RECKON_FOLLOWER_IMAGE_COMMIT"


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


def _commit_file(root: Path, relative: str, text: str, message: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(root, "add", relative)
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _identity(root: Path) -> str:
    return checkout_trust.source_code_identity(root, source_root=root)


def _executed_root(execs) -> Path:
    launcher = execs[0][1][2]
    literal = launcher.split("sys.path.insert(0, ", 1)[1].split(");", 1)[0]
    return Path(ast.literal_eval(literal))


@pytest.fixture()
def source(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        checkout_trust,
        "_committed_tree_cache",
        lambda: tmp_path / "home" / "crew" / "reload-trees",
    )
    root = tmp_path / "source"
    (root / "reckon").mkdir(parents=True)
    (root / "reckon" / "__init__.py").write_text("")
    (root / "reckon" / "example.py").write_text("VALUE = 'first'\n")
    _git(root, "init")
    _git(root, "config", "user.name", "Identity Test")
    _git(root, "config", "user.email", "identity@example.invalid")
    _git(root, "add", "reckon/__init__.py", "reckon/example.py")
    _git(root, "commit", "-m", "First package")
    monkeypatch.setattr(follow, "_import_root", lambda module: root)
    monkeypatch.setattr(
        runs,
        "follower_code_stamp",
        lambda: checkout_trust.source_code_identity(root, source_root=root),
    )
    monkeypatch.setattr(runs, "FOLLOWER_FRESHNESS_SECONDS", 0)
    monkeypatch.setattr(
        import_module("reckon.crew.dispatch"),
        "_export_launched_workers_for_reexec",
        lambda: None,
    )
    execs: list = []
    monkeypatch.setattr(follow.os, "execve", lambda *args: execs.append(args))
    return root, execs


def test_source_identity_is_the_reckon_tree_not_the_commit(source):
    root, _ = source
    assert _identity(root) == _git(root, "rev-parse", "HEAD:reckon")
    assert _identity(root) != _git(root, "rev-parse", "HEAD")


def test_a_commit_outside_reckon_moves_no_identity_and_no_reload(source, capsys):
    """(a) A commit touching only a file outside reckon/ changes nothing."""
    root, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    before = reloader.code_stamp
    assert before == _identity(root)

    _commit_file(root, "docs/plan.html", "<p>plan</p>\n", "Plan edit outside package")

    reloader.poll({})

    assert _identity(root) == before
    assert reloader.code_stamp == before
    assert execs == []
    assert "deferred" not in capsys.readouterr().out
    cache = checkout_trust._committed_tree_cache()
    assert not cache.exists() or not any(cache.rglob(".complete"))


def test_a_commit_under_reckon_moves_identity_and_materializes_a_tree(source):
    """(b) A commit changing a file under reckon/ moves it and reloads."""
    root, execs = source
    reloader = follow._FollowerReloader("test-project", None)
    before = reloader.code_stamp

    _commit_file(root, "reckon/example.py", "VALUE = 'second'\n", "Change package file")
    after = _identity(root)
    assert after != before

    reloader.poll({})

    assert len(execs) == 1
    image = _executed_root(execs)
    assert image.name == after
    assert (image / "reckon" / "__init__.py").is_file()
    assert (image / "reckon" / "example.py").read_text() == "VALUE = 'second'\n"
    assert execs[0][2][_IMAGE_COMMIT_ENV] == after


def test_a_follower_and_a_producer_on_the_same_package_compare_equal(
    source, monkeypatch
):
    """(c) Two commits with the same reckon/ content share one identity."""
    root, _ = source
    first_identity = _identity(root)
    first_commit = _git(root, "rev-parse", "HEAD")
    image, _, _, pin = checkout_trust.materialize_committed_tree(root, first_identity)
    os.close(pin)
    monkeypatch.setenv(_IMAGE_COMMIT_ENV, first_identity)
    producer_identity = checkout_trust.running_code_identity(image / "reckon")

    _commit_file(root, "NOTES.md", "outside the package\n", "Notes outside package")

    follower_identity = _identity(root)
    assert follower_identity == producer_identity
    assert producer_identity != first_commit


def test_materialize_extracts_only_the_package_under_its_prefix(source):
    root, _ = source
    _commit_file(root, "NOTES.md", "outside the package\n", "Notes outside package")
    identity = _identity(root)

    image, _, _, pin = checkout_trust.materialize_committed_tree(root, identity)
    os.close(pin)

    assert image.name == identity
    assert (image / "reckon" / "__init__.py").is_file()
    assert not (image / "NOTES.md").exists()
    # Only the package and the image's own bookkeeping; no repository file that
    # lives beside reckon/ is carried into the reload tree.
    bookkeeping = {"reckon", ".complete", ".in-use", ".lease"}
    assert {entry.name for entry in image.iterdir()} <= bookkeeping


def test_an_image_carrying_a_commit_identity_reloads_once(source, monkeypatch, capsys):
    """An image armed before the identity named the package still reloads."""
    root, execs = source
    old_commit = _git(root, "rev-parse", "HEAD")
    monkeypatch.setenv(_IMAGE_COMMIT_ENV, old_commit)
    reloader = follow._FollowerReloader("test-project", None)
    assert reloader.code_stamp == old_commit

    reloader.poll({})

    out = capsys.readouterr().out
    assert "deferred" not in out and "could not" not in out
    assert len(execs) == 1
    assert execs[0][2][_IMAGE_COMMIT_ENV] == _identity(root)
