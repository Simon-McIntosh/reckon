"""The reload image cache lives under the node-local runtime root.

An image is shared by every process on one node and by nothing off it, so the
cache resolves through the station's own runtime-root resolver: a ``flock``
taken on an image by one reader is visible to every pruner here, and no
cross-node lease record is needed to keep one. A cache under the crew home
would put the images back on shared storage, which is what the lease record
existed to make safe.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon import checkout_trust
from reckon.crew import obligation_snapshot, station_store

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


@pytest.fixture()
def runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    return root


def _cache_path() -> Path:
    return checkout_trust._committed_tree_cache()


def _assert_the_cache_is_node_local(root: Path) -> None:
    """The path test: the cache is under ``root`` and never under the home."""
    cache = _cache_path()
    assert cache == root / "reload-trees", cache
    assert not cache.is_relative_to(obligation_snapshot.crew_home()), cache


def test_the_cache_resolves_under_the_runtime_root(runtime_root, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    _assert_the_cache_is_node_local(runtime_root)


def test_the_cache_follows_the_root_the_station_resolves(runtime_root, monkeypatch):
    """One resolver: the cache and the station cannot name different roots."""
    monkeypatch.setattr(
        station_store,
        "runtime_root_source",
        lambda **kwargs: (runtime_root, "per-user-runtime"),
    )
    _assert_the_cache_is_node_local(runtime_root)


def test_a_shared_runtime_root_is_refused(runtime_root, monkeypatch):
    """A root on shared storage is where ``flock`` stops being decisive.

    The root is one the station itself would refuse, so the cache is refused
    with the station's own refusal rather than publishing images where a
    pruner on another node reads them.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(
        station_store, "filesystem_type", lambda root, *, mounts: "gpfs"
    )
    with pytest.raises(
        station_store.StationStoreError, match="refusing the station's node-local root"
    ):
        _cache_path()


def test_the_cache_asks_the_station_resolver_and_nothing_else(monkeypatch):
    """A second resolver would answer where this one was never asked."""

    def refuse(**kwargs):
        raise RuntimeError("the station's resolver was asked")

    monkeypatch.setattr(station_store, "runtime_root_source", refuse)
    with pytest.raises(RuntimeError, match="the station's resolver was asked"):
        _cache_path()


def test_no_runtime_root_is_refused_rather_than_fallen_back_to_the_home(
    monkeypatch,
):
    monkeypatch.setattr(
        station_store, "runtime_root_source", lambda **kwargs: (None, "none")
    )
    with pytest.raises(RuntimeError, match="never the crew home"):
        _cache_path()


def test_a_published_image_lands_under_the_runtime_root(
    runtime_root, tmp_path, monkeypatch
):
    """The cache the materialiser writes to is the node-local one."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    root = tmp_path / "source"
    (root / "reckon").mkdir(parents=True)
    (root / "reckon" / "__init__.py").write_text("")
    _git(root, "init")
    _git(root, "config", "user.name", "Reload Test")
    _git(root, "config", "user.email", "reload@example.invalid")
    _git(root, "add", "reckon/__init__.py")
    _git(root, "commit", "-m", "First")
    commit = _git(root, "rev-parse", "HEAD")

    image, _, _, pin = checkout_trust.materialize_committed_tree(root, commit)
    try:
        assert image.is_dir()
        assert image.is_relative_to(runtime_root)
        assert not image.is_relative_to(obligation_snapshot.crew_home())
    finally:
        os.close(pin)
