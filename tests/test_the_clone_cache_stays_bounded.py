"""The clone scan's corpus cache holds a bounded number of entries.

Every content a corpus file ever had keeps its own entry, keyed by the file's
path and content digest, so a cache root shared by many revisions and worktrees
accumulates an entry per revision. The cache therefore records when each entry
was last used and prunes the least recently used once it holds more than one
entry per corpus file. These cases drive scans over more distinct revisions
than that cap admits and assert the entry count stays at or below the cap, that
the latest scan's entry survives the prune, and that the bounded cache still
reports exactly what a cold full scan reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import clones

# A function with more than a six-line window, and a copy whose only difference
# is its name, so the copy's window that omits the ``def`` line hashes alike.
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

# A revision's unrelated files carry one distinct body per revision and index,
# so each revision contributes that many fresh cache entries. They are short, so
# they produce no six-line window and cannot match anything; they exist only to
# fill the cache.
UNRELATED_COUNT = 4
REVISIONS = 6
CORPUS_FILE_COUNT = 2 + UNRELATED_COUNT


def _unrelated(revision: int, index: int) -> str:
    return (
        f"def tally_{revision}_{index}(values):\n"
        f"    total = sum(values) + {revision * 10 + index}\n"
        "    return total\n"
    )


def _unrelated_path(index: int) -> str:
    return f"reckon/unrelated_{index}.py"


def _head(revision: int) -> dict[str, str]:
    head = {ORIGINAL_PATH: ORIGINAL, COPY_PATH: COPY}
    for index in range(UNRELATED_COUNT):
        head[_unrelated_path(index)] = _unrelated(revision, index)
    return head


@pytest.fixture()
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "clone-cache"
    monkeypatch.setenv("RECKON_CLONE_CACHE", str(root))
    clones._CORPUS_CACHES.clear()
    return root


def _scan_revisions(cache_root: Path) -> None:
    """Scan one more distinct revision than the cap admits, warm-cached."""
    for revision in range(REVISIONS):
        clones.clone_matches(
            _head(revision), changed_paths=[COPY_PATH], base_sources={}
        )


def _loaded(cache_root: Path) -> dict[str, object]:
    clones._CORPUS_CACHES.clear()
    return clones._load_corpus_cache(cache_root)


def test_the_cache_holds_at_most_one_revision_of_entries(cache_root: Path) -> None:
    """After REVISIONS distinct revisions share the root, the cache holds no
    more than one entry per corpus file, and an earlier revision's entry was
    actually dropped rather than the bound passing vacuously."""
    _scan_revisions(cache_root)
    stored = _loaded(cache_root)

    assert len(stored) <= clones._cap_for(CORPUS_FILE_COUNT)
    assert len(stored) == CORPUS_FILE_COUNT, "the cap should be reached"
    dropped = clones._cache_key(_unrelated_path(0), _unrelated(0, 0))
    assert dropped not in stored, "an earlier revision's entry must be evicted"


def test_an_entry_used_by_the_latest_scan_survives_eviction(cache_root: Path) -> None:
    """The prune keeps the most recently used entries, so everything the latest
    scan read is still there for it."""
    _scan_revisions(cache_root)
    stored = _loaded(cache_root)
    latest = REVISIONS - 1

    assert clones._cache_key(ORIGINAL_PATH, ORIGINAL) in stored
    assert clones._cache_key(COPY_PATH, COPY) in stored
    for index in range(UNRELATED_COUNT):
        path = _unrelated_path(index)
        assert clones._cache_key(path, _unrelated(latest, index)) in stored


def test_the_bounded_cache_reports_the_full_scan_matches(
    cache_root: Path, tmp_path: Path
) -> None:
    """Pruning must not change the reading: the bounded warm cache reports
    exactly the matches a cold full scan reports."""
    _scan_revisions(cache_root)
    final = _head(REVISIONS - 1)

    warm = clones.clone_matches(final, changed_paths=[COPY_PATH], base_sources={})
    cold = clones.clone_matches(
        final,
        changed_paths=[COPY_PATH],
        base_sources={},
        cache_root=tmp_path / "cold-cache",
    )

    assert cold, "the fixture copy must be reported as a match"
    assert warm == cold


def test_the_cap_derives_from_the_corpus_file_count() -> None:
    """The cap scales with the corpus rather than being a fixed literal."""
    assert clones._cap_for(200) > clones._cap_for(20)
    assert clones._cap_for(50) > clones._cap_for(5)
    assert clones._cap_for(0) >= 1
