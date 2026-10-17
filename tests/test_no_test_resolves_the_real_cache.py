"""No test resolves the real cache.

A module- or session-scoped fixture is set up before the per-test
``isolated_reckon_home``, so a cache root resolved while one runs fell through
to the operator's real ``~/.cache/reckon`` and left a ledger index there, keyed
to a temporary repository pytest then removed. The session fixture in
``conftest`` redirects every root before those fixtures run, and a wrapper
around ``_store.cache_root`` records any ambient resolution that still leaves
the session tree.

This test reads that record. It is collected in place — before the module it
watches — and moved to the end of the run by ``pytest_collection_modifyitems``,
so the record is complete when it reads it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _store
from tests.conftest import cache_root_leaks


def _within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root.resolve() or root.resolve() in resolved.parents


def test_no_test_resolves_the_real_cache(tmp_path_factory) -> None:
    """Every recorded ``cache_root`` resolution stayed inside the session temp.

    The record is only meaningful if the wrapper is watching, so the guard's
    own state is asserted first: an empty record from a wrapper that was never
    installed would pass while hiding the leak it exists to name.
    """
    assert getattr(_store.cache_root, "_resolves_real_cache_guard", False), (
        "the cache_root wrapper was never installed, so an empty leak record "
        "proves nothing"
    )
    session_root = tmp_path_factory.getbasetemp()
    assert _within(_store.cache_root("pick-input"), session_root), (
        "the ambient pick-input cache root is not inside the session temp tree"
    )
    assert _within(_store.cache_root("velocity"), session_root)

    leaks = cache_root_leaks()
    if leaks:
        named = "\n".join(
            f"  {leak['kind']} -> {leak['resolved']}\n"
            f"    from {leak['caller']}"
            + (f"\n    during {leak['nodeid']}" if leak["nodeid"] else "")
            for leak in leaks
        )
        pytest.fail(
            "a test or fixture resolved a cache root outside the session temp "
            f"tree:\n{named}\n"
            "point the root at a session temporary directory before the "
            "module- or session-scoped fixture runs."
        )
