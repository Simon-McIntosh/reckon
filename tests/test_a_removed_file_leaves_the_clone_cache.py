"""A file removed from the corpus stops holding a path in the clone cache.

The corpus fingerprint cache is bounded to one entry per corpus file. A path
present in the cache counted toward that cap unconditionally, so a file removed
or renamed out of the corpus kept its own entry in the count and pinned the cap
open: neither the removed path nor a superseded revision of a live path could
ever be evicted, and ``corpus.json`` grew with every path ever scanned rather
than with the current corpus. A cached path now counts toward the cap only while
a recent scan read it, so a path no scan has read for a stated span of scans
stops counting and its entry is evicted.

These cases scan a corpus, remove one file, run scans past the span, and assert
the removed path's entry is gone while the cache holds exactly the current
corpus. They also keep the partial-scan guarantee: a scan of part of the corpus
does not evict the warm entries the next full scan would reuse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import clones

# Two functions whose bodies differ only in their names, so the window that
# omits the ``def`` line hashes alike and the copy is reported as a match.
ORIGINAL = """def parse_utc(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        return _from_iso8601(value)
    return None
"""

COPY = """def duplicate_parse(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        return _from_iso8601(value)
    return None
"""

ORIGINAL_PATH = "reckon/_timestamps.py"
COPY_PATH = "reckon/private_copy.py"


def _filler(index: int) -> str:
    return (
        f"def helper_{index}(values):\n"
        "    if not values:\n"
        "        return None\n"
        f"    scaled = [value * {index + 1} for value in values]\n"
        "    total = sum(scaled)\n"
        "    return total\n"
    )


def _filler_path(index: int) -> str:
    return f"reckon/helper_{index}.py"


FILLER_COUNT = 3
# The path removed from the corpus once it has been scanned and cached.
REMOVED_PATH = _filler_path(0)
CORPUS_PATHS = [ORIGINAL_PATH, COPY_PATH] + [
    _filler_path(index) for index in range(FILLER_COUNT)
]


def _full_corpus() -> dict[str, str]:
    head = {ORIGINAL_PATH: ORIGINAL, COPY_PATH: COPY}
    for index in range(FILLER_COUNT):
        head[_filler_path(index)] = _filler(index)
    return head


def _corpus_without_removed() -> dict[str, str]:
    return {
        path: source
        for path, source in _full_corpus().items()
        if path != REMOVED_PATH
    }


@pytest.fixture()
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "clone-cache"
    monkeypatch.setenv("RECKON_CLONE_CACHE", str(root))
    clones._CORPUS_CACHES.clear()
    return root


def _stored(cache_root: Path) -> dict[str, object]:
    clones._CORPUS_CACHES.clear()
    return clones._load_corpus_cache(cache_root)


def _held_paths(stored: dict[str, object]) -> set[str]:
    return {
        path
        for entry in stored.values()
        if (path := clones._entry_path(entry)) is not None
    }


def test_a_removed_file_leaves_no_cache_entry(cache_root: Path) -> None:
    """A file scanned once and then removed from the corpus must not survive in
    the cache past the staleness span: its entry is evicted and the cache holds
    only the current corpus."""
    clones.clone_matches(_full_corpus(), changed_paths=[], base_sources={})
    assert set(CORPUS_PATHS) <= _held_paths(_stored(cache_root)), (
        "every scanned corpus path must be cached before the removal"
    )

    remaining = _corpus_without_removed()
    for _ in range(clones._STALE_PATH_SCANS + 1):
        clones.clone_matches(remaining, changed_paths=[], base_sources={})

    stored = _stored(cache_root)
    assert REMOVED_PATH not in _held_paths(stored), (
        "the removed path's entry must be evicted once no scan reads it"
    )
    assert len(stored) == len(remaining), (
        "the cache must hold exactly one entry per current corpus file; "
        f"expected {len(remaining)}, got {len(stored)}"
    )


def test_the_partial_scan_still_re_parses_nothing(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full scan, a partial scan, and a full scan again must re-parse nothing:
    the staleness span leaves a still-present path's warm entry in place."""
    clones.clone_matches(_full_corpus(), changed_paths=[], base_sources={})
    clones.clone_matches({ORIGINAL_PATH: ORIGINAL}, changed_paths=[], base_sources={})

    parsed: list[str] = []
    real = clones.functions_in

    def counting(source: str, path: str):
        parsed.append(path)
        return real(source, path)

    monkeypatch.setattr(clones, "functions_in", counting)
    clones.clone_matches(_full_corpus(), changed_paths=[], base_sources={})
    assert parsed == [], f"the warm corpus must not be re-parsed, parsed: {parsed}"