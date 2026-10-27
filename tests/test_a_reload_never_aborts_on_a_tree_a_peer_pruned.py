"""A prune tolerates an entry a peer pruned out from under it.

Two followers reloading during HEAD churn prune the same cache at once, so the
entry one is ranking or removing can already be gone. The publish grace covers
that churn window: only ``materialize_committed_tree`` writes a tree's
directory mtime, through ``os.utime(target)``, so every follower that
materialises a head touches that head's tree and a tree published moments ago
can rank past the newest few. The tests below hold the grace to
``COMMITTED_TREE_PUBLISH_GRACE_SECONDS`` (300 s), above the 137 s of churn
measured when four commits landed in that span on 2026-10-17.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew_follow_commands as follow

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


def _cache_dir(root: Path) -> Path:
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    cache = follow.checkout_trust._committed_tree_cache() / key
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _publish(root: Path, commit: str) -> Path:
    image, _, _, pin = follow.checkout_trust.materialize_committed_tree(root, commit)
    os.close(pin)
    return image


def _seed_image(
    cache: Path, name: str, *, age: float | None = None, in_use: bool = True
) -> Path:
    """Place an image in the cache directly, skipping the prune a publish runs.

    A real publish prunes the cache, so building a state where one image must
    survive a later prune has to place the image without that side effect.
    """
    image = cache / name
    (image / "reckon").mkdir(parents=True)
    (image / "reckon" / "__init__.py").write_text("")
    (image / ".complete").touch()
    if in_use:
        (image / ".in-use").touch()
    if age is not None:
        moment = time.time() - age
        os.utime(image, (moment, moment))
        os.utime(image / ".complete", (moment, moment))
    return image


@pytest.fixture()
def source(tmp_path: Path, monkeypatch):
    # A test run from inside a materialised image carries the image's own
    # source root and commit; clear them so these assertions depend on the
    # fixture rather than on how the suite was launched.
    for carried in (
        follow._FOLLOWER_SOURCE_ROOT_ENV,
        follow._FOLLOWER_IMAGE_COMMIT_ENV,
        "RECKON_FOLLOWER_OWNER",
    ):
        monkeypatch.delenv(carried, raising=False)
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
    return root, first


def _three_fillers(root: Path) -> None:
    checkout = follow.checkout_trust
    for index in range(checkout.COMMITTED_TREE_RETENTION):
        _publish(root, _commit(root, f"filler-{index}"))


def test_two_materialisers_pruning_one_entry_both_return_pinned_trees(
    source, monkeypatch
):
    """A peer that removes the entry first is not an error for this pruner."""
    root, _ = source
    checkout = follow.checkout_trust
    _three_fillers(root)
    old = _seed_image(_cache_dir(root), "old", age=1000.0)
    target_commit = _commit(root, "target")

    real_keep = checkout._prune_must_keep
    peer_images: list[Path] = []
    peer_ran: list[bool] = []

    def keep_or_run_a_peer(entry: Path) -> bool:
        if entry == old and not peer_ran:
            # A second materialiser publishes newer images and its own prune
            # takes the same old entry out from under this one.
            peer_ran.append(True)
            peer_images.extend(
                _publish(root, _commit(root, f"peer-{index}"))
                for index in range(checkout.COMMITTED_TREE_RETENTION + 1)
            )
        return real_keep(entry)

    monkeypatch.setattr(checkout, "_prune_must_keep", keep_or_run_a_peer)
    image, removed, _, pin = checkout.materialize_committed_tree(root, target_commit)
    try:
        assert peer_ran, "the peer materialiser never ran"
        assert image.is_dir(), "the materialiser lost its own pinned tree"
        assert old in removed, "a tree a peer removed still counts as removed"
        assert not old.exists()
        assert peer_images, "the peer returned no image"
        assert peer_images[-1].is_dir()
    finally:
        os.close(pin)


def test_an_entry_vanishing_before_its_stat_key_is_skipped(source, monkeypatch):
    """An entry gone between the listing and its stat key is not an error."""
    root, _ = source
    checkout = follow.checkout_trust
    _three_fillers(root)
    ghost = _seed_image(_cache_dir(root), "ghost", age=1000.0)
    target_commit = _commit(root, "target")

    real_stat = Path.stat

    def stat_that_loses_the_ghost(self, *args, **kwargs):
        if self == ghost:
            shutil.rmtree(self)
            raise FileNotFoundError(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_that_loses_the_ghost)
    image, removed, kept, pin = checkout.materialize_committed_tree(root, target_commit)
    try:
        assert image.is_dir()
        assert not ghost.exists()
        assert ghost not in removed
        assert ghost not in kept
    finally:
        os.close(pin)


def test_a_tree_inside_the_churn_window_survives_a_churned_ranking(source):
    """A tree published 137 s ago outranks none, but the grace keeps it."""
    root, _ = source
    checkout = follow.checkout_trust
    cache = _cache_dir(root)
    recent = _seed_image(cache, "recent", age=137.0, in_use=False)

    _three_fillers(root)
    _, _, kept, pin = checkout.materialize_committed_tree(root, _commit(root, "target"))
    try:
        assert recent.is_dir(), "a tree inside the churn window was pruned"
        assert recent in kept
    finally:
        os.close(pin)


def test_a_tree_past_the_churn_window_is_pruned_as_before(source):
    """Past the grace the tree is reclaimed exactly as before."""
    root, _ = source
    checkout = follow.checkout_trust
    cache = _cache_dir(root)
    old = _seed_image(
        cache,
        "long-ago",
        age=checkout.COMMITTED_TREE_PUBLISH_GRACE_SECONDS + 60.0,
        in_use=False,
    )

    _three_fillers(root)

    assert not old.exists(), "a tree past the publish grace was spared"


def test_a_tree_removed_under_the_prune_walk_is_skipped(source, monkeypatch):
    """A tree a peer removes while this pruner walks it is not an error.

    A peer's concurrent removal surfaces as ENOTDIR on a component inside the
    tree (``.../reckon/crew/prompts`` in the follower log), which the prune
    must treat as a tree already removed rather than an aborted reload.
    """
    root, _ = source
    checkout = follow.checkout_trust
    _three_fillers(root)
    old = _seed_image(_cache_dir(root), "old", age=1000.0)
    target_commit = _commit(root, "target")
    inner = old / "reckon" / "crew" / "prompts"

    real_rmtree = shutil.rmtree
    raced: list[bool] = []

    def rmtree_raced_by_a_peer(path, *args, **kwargs):
        if Path(path) == old and not raced:
            raced.append(True)
            real_rmtree(path)
            raise NotADirectoryError(20, "Not a directory", str(inner))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(checkout.shutil, "rmtree", rmtree_raced_by_a_peer)
    image, removed, _, pin = checkout.materialize_committed_tree(root, target_commit)
    try:
        assert raced, "the prune walk was never raced"
        assert image.is_dir(), "the materialiser lost its own pinned tree"
        assert old in removed
        assert not old.exists()
    finally:
        os.close(pin)
