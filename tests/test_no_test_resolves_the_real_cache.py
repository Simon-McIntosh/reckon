"""No test resolves the real cache.

A module- or session-scoped fixture is set up before the per-test
``isolated_reckon_home``, so a cache root resolved while one runs fell through
to the operator's real ``~/.cache/reckon`` and left a ledger index there, keyed
to a temporary repository pytest then removed. The session fixture in
``conftest`` redirects ``XDG_CACHE_HOME`` — the one lever every cache kind
reads — before those fixtures run, and a wrapper around ``_store.cache_root``
records any ambient resolution that still lands outside the run's temp tree.

This test reads that record. It is collected in place — before the module it
watches too — and moved to the end of the run by
``pytest_collection_modifyitems``, so the record is complete when it reads it.

The wrapper is installed when ``conftest`` is imported, which is a point every
xdist worker reaches in its own process. A leak a worker records fails that
worker's own session when the fixture tears down, so a leak is reported whether
or not this test is scheduled beside the test that caused it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon import _store
from tests.conftest import cache_root_leak_report, cache_root_leaks

# Kinds that read the cache home alone, so the session redirect moves them
# whatever else is set. The two kinds absent from this tuple (``pick-input``
# and ``run-time-profile``) read ``RECKON_HOME`` first, which a test is free to
# set to steer a root of its own.
CACHE_HOME_KINDS = ("velocity", "clones", "session-host", "client")

# The variable that arms the positive control below. Left unset, the whole
# suite runs without it; the run that proves the guard sees what it exists to
# see sets it.
POSITIVE_CONTROL_ENV = "RECKON_CACHE_GUARD_POSITIVE_CONTROL"


def _within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root.resolve() or root.resolve() in resolved.parents


def test_no_test_resolves_the_real_cache(tmp_path_factory) -> None:
    """Every recorded ``cache_root`` resolution stayed inside the run's temp tree."""
    session_root = tmp_path_factory.getbasetemp()
    assert getattr(_store.cache_root, "_resolves_real_cache_guard", False), (
        "the cache_root wrapper was never installed, so an empty leak record "
        "proves nothing"
    )
    for kind in CACHE_HOME_KINDS:
        assert _within(_store.cache_root(kind), session_root), (
            f"the ambient {kind} cache root is not inside the run's temp tree"
        )

    reckon_home = os.environ.get("RECKON_HOME")
    for kind in ("pick-input", "run-time-profile"):
        resolved = _store.cache_root(kind)
        steered = bool(reckon_home) and _within(resolved, Path(reckon_home))
        assert steered or _within(resolved, session_root), (
            f"the ambient {kind} cache root is inside neither the run's temp "
            "tree nor the RECKON_HOME a test set"
        )

    if cache_root_leaks():
        pytest.fail(cache_root_leak_report())


@pytest.mark.skipif(
    not os.environ.get(POSITIVE_CONTROL_ENV),
    reason=f"armed by {POSITIVE_CONTROL_ENV}, so the suite is not run with it",
)
def test_positive_control_a_test_resolves_the_real_cache(monkeypatch) -> None:
    """Resolve the real cache, on purpose, so the guard has something to name.

    The guard is only evidence if it reports a resolution that left the run's
    temp tree, so this test deliberately makes one: with the redirect's two
    levers removed, ``cache_root`` falls back to the home directory and lands
    in the live cache. ``cache_root`` returns a path and creates nothing, so
    this resolves the real root without writing to it. The suite run that arms
    this test must name it.
    """
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("RECKON_HOME", raising=False)

    resolved = _store.cache_root("velocity")
    assert Path.home() / ".cache" in resolved.resolve().parents, (
        "the positive control did not leave the run's temp tree"
    )
