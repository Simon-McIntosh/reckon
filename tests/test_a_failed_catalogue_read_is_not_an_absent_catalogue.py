"""A failed catalogue read is not an absent catalogue.

The code identity folds the model catalogue, with the package and its scripts,
into one content tree naming what a reloading process will run. A wheel install
carries no catalogue, so an identity for a checkout without one is legitimate --
but a read that *failed* is a different fact. Read as absence, one timed-out
``git`` call yields the catalogue-less tree, a valid-looking identity every
follower and producer then reloads onto, launching with no model named until the
same read next succeeds and moves the identity back. So a read that did not
answer leaves the identity unknown, and a reloader that reads an unknown
identity keeps the image it is running.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from reckon import checkout_trust
from reckon import crew_follow_commands as follow
from reckon.crew import runs

CATALOGUE_RELATIVE_PATH = "docs/state/reckon/model-catalogue.yaml"
IMAGE_COMMIT_ENV = "RECKON_FOLLOWER_IMAGE_COMMIT"

_CATALOGUE = (
    "version: 1\n"
    "backends:\n"
    "  clive:\n"
    "    model: deepseek-v4.1-flash\n"
    '    alias: "dsv4.1-flash"\n'
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _source(tmp_path: Path, *, catalogue: bool) -> Path:
    """A temporary checkout: the package and its script, and optionally the catalogue."""
    root = tmp_path / "source"
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(
        source / "reckon",
        root / "reckon",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = root / "skills/reckon-build/scripts/worktree_fleet.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(source / "skills/reckon-build/scripts/worktree_fleet.py", script)
    if catalogue:
        target = root / CATALOGUE_RELATIVE_PATH
        target.parent.mkdir(parents=True)
        target.write_text(_CATALOGUE)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Catalogue Read Test")
    _git(root, "config", "user.email", "catalogue@example.invalid")
    paths = ["reckon", "skills", "docs"] if catalogue else ["reckon", "skills"]
    _git(root, "add", *paths)
    _git(root, "commit", "-qm", "Seed source")
    return root


def _catalogue_less_tree(root: Path) -> str:
    """The identity a git repo without a catalogue yields: package and scripts."""
    entries = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "HEAD", "reckon", "skills"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return checkout_trust._mktree(root, entries)


def _timing_out_catalogue_read(monkeypatch) -> None:
    """Make any git read of the catalogue raise a timeout; leave every other read."""
    real_run = checkout_trust.subprocess.run

    def _run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and any(
            "model-catalogue" in str(part) for part in argv
        ):
            raise subprocess.TimeoutExpired(argv, checkout_trust.IMPORT_PROBE_TIMEOUT)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(checkout_trust.subprocess, "run", _run)


def test_a_timed_out_catalogue_read_reports_unknown_not_an_absent_catalogue(
    tmp_path: Path, monkeypatch
) -> None:
    """The catalogue is present and its read times out: the identity is unknown."""
    root = _source(tmp_path, catalogue=True)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    folded = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    assert folded != checkout_trust.IDENTITY_UNKNOWN
    assert folded != _catalogue_less_tree(root)

    _timing_out_catalogue_read(monkeypatch)
    reading = checkout_trust.source_code_identity(root / "reckon", source_root=root)

    assert reading == checkout_trust.IDENTITY_UNKNOWN
    # The defect this guards: a failed read folding into the catalogue-less tree.
    assert reading != _catalogue_less_tree(root)


def test_a_checkout_without_the_catalogue_still_yields_the_package_tree(
    tmp_path: Path, monkeypatch
) -> None:
    """A repo that genuinely has no catalogue at HEAD: package and scripts."""
    root = _source(tmp_path, catalogue=False)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    identity = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    assert identity == _catalogue_less_tree(root)
    assert identity != checkout_trust.IDENTITY_UNKNOWN


def test_a_successful_read_folds_the_catalogue_in(tmp_path: Path, monkeypatch) -> None:
    """Every read succeeds with the catalogue present: it is folded in as today."""
    root = _source(tmp_path, catalogue=True)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    identity = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    assert identity != checkout_trust.IDENTITY_UNKNOWN
    assert identity != _catalogue_less_tree(root)
    image, _, _, pin = checkout_trust.materialize_committed_tree(root, identity)
    os.close(pin)
    assert (image / CATALOGUE_RELATIVE_PATH).is_file()


def test_a_reloader_given_an_unknown_identity_keeps_its_recorded_stamp(
    tmp_path: Path, monkeypatch
) -> None:
    """An unknown reading is no new identity: the reloader keeps its image."""
    root = _source(tmp_path, catalogue=True)
    monkeypatch.setattr(
        checkout_trust, "_committed_tree_cache", lambda: tmp_path / "images"
    )
    monkeypatch.setattr(follow, "_import_root", lambda module: root)
    monkeypatch.setattr(runs, "FOLLOWER_FRESHNESS_SECONDS", 0)
    monkeypatch.delenv(IMAGE_COMMIT_ENV, raising=False)

    good = checkout_trust.source_code_identity(root / "reckon", source_root=root)
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: good)
    reloader = follow._FollowerReloader("test-project", None)
    recorded = reloader.code_stamp
    assert recorded == good

    def _no_materialize(*args, **kwargs):
        raise AssertionError("an unknown identity must not materialise a tree")

    execs: list = []
    monkeypatch.setattr(checkout_trust, "materialize_committed_tree", _no_materialize)
    monkeypatch.setattr(follow.os, "execve", lambda *args: execs.append(args))
    monkeypatch.setattr(
        runs, "follower_code_stamp", lambda: checkout_trust.IDENTITY_UNKNOWN
    )

    reloader.poll({})

    assert reloader.code_stamp == recorded
    assert execs == []


def tmp_path_images(tmp_path: Path) -> Path:
    """The reload-tree cache for a test: one root, named once so every read agrees."""
    return tmp_path / "images"
