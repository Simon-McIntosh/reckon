"""A scan over part of the corpus keeps the whole cache warm.

The clone scan's corpus cache is pruned to one entry per corpus file it knows
about. A caller that hands the scan only a subset of the corpus — one changed
file, say — must not shrink that cap to the subset it read, or the prune evicts
the warm entries the next full scan would have reused and every plan of that
scan is parsed again. These cases scan five corpus files, then one, then five
again, and assert nothing is re-parsed; and they drive more distinct revisions
of the corpus than the cap admits and assert the cache still holds at most one
entry per path.
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
CORPUS_PATHS = (
    ORIGINAL_PATH,
    COPY_PATH,
    *(_filler_path(i) for i in range(FILLER_COUNT)),
)


def _full_corpus() -> dict[str, str]:
    head = {ORIGINAL_PATH: ORIGINAL, COPY_PATH: COPY}
    for index in range(FILLER_COUNT):
        head[_filler_path(index)] = _filler(index)
    return head


@pytest.fixture()
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "clone-cache"
    monkeypatch.setenv("RECKON_CLONE_CACHE", str(root))
    clones._CORPUS_CACHES.clear()
    return root


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    parsed: list[str] = []
    real = clones.functions_in

    def counting(source: str, path: str):
        parsed.append(path)
        return real(source, path)

    monkeypatch.setattr(clones, "functions_in", counting)
    return parsed


def _stored(cache_root: Path) -> dict[str, object]:
    clones._CORPUS_CACHES.clear()
    return clones._load_corpus_cache(cache_root)


def test_a_partial_scan_does_not_evict_the_rest_of_the_corpus(
    cache_root: Path,
) -> None:
    """A five-file scan fills the cache; a one-file scan must leave the other
    four entries in place rather than pruning the cache to the subset it read."""
    clones.clone_matches(_full_corpus(), changed_paths=[], base_sources={})
    assert len(_stored(cache_root)) == len(CORPUS_PATHS)

    clones.clone_matches({ORIGINAL_PATH: ORIGINAL}, changed_paths=[], base_sources={})
    assert len(_stored(cache_root)) == len(CORPUS_PATHS), (
        "a scan of one file must not evict the other files' warm entries"
    )


def test_the_next_full_scan_re_parses_nothing(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a full scan, a partial scan, and a full scan again, the corpus
    whose bytes never changed is read from the cache: nothing is re-parsed."""
    clones.clone_matches(_full_corpus(), changed_paths=[], base_sources={})
    clones.clone_matches({ORIGINAL_PATH: ORIGINAL}, changed_paths=[], base_sources={})

    parsed = _count_parses(monkeypatch)
    clones.clone_matches(_full_corpus(), changed_paths=[], base_sources={})
    assert parsed == [], f"the warm corpus must not be re-parsed, parsed: {parsed}"


def test_the_partial_scan_still_reports_the_full_scan_matches(cache_root: Path) -> None:
    """Retention must not change the reading: after the partial scan, a scan of
    the whole corpus reports exactly what a cold full scan reports."""
    clones.clone_matches(_full_corpus(), changed_paths=[], base_sources={})
    clones.clone_matches({ORIGINAL_PATH: ORIGINAL}, changed_paths=[], base_sources={})

    full = _full_corpus()
    warm = clones.clone_matches(full, changed_paths=[COPY_PATH], base_sources={})
    cold = clones.clone_matches(
        full,
        changed_paths=[COPY_PATH],
        base_sources={},
        cache_root=cache_root / "cold",
    )
    assert cold, "the fixture copy must be reported as a match"
    assert warm == cold


# A corpus of a fixed two paths, scanned at more distinct revisions than the cap
# admits, to show the prune still bounds the cache to one entry per path.
REVISIONS = 6
BOUNDED_PATHS = (ORIGINAL_PATH, COPY_PATH)


def _revision(revision: int) -> dict[str, str]:
    return {
        ORIGINAL_PATH: ORIGINAL + f"\n# revision {revision}\n",
        COPY_PATH: COPY,
    }


def test_scans_over_many_revisions_hold_one_entry_per_path(cache_root: Path) -> None:
    """More distinct revisions than there are corpus paths must still leave at
    most one entry per path — the most recently used — after the prune."""
    for revision in range(REVISIONS):
        clones.clone_matches(_revision(revision), changed_paths=[], base_sources={})

    stored = _stored(cache_root)
    assert len(stored) <= len(BOUNDED_PATHS)
    for path in BOUNDED_PATHS:
        held = [key for key, value in stored.items() if _entry_names(value, path)]
        assert len(held) <= 1, f"{path} must hold at most one entry, got {held}"


def _entry_names(entry: object, path: str) -> bool:
    if not isinstance(entry, dict):
        return False
    functions = entry.get("functions")
    return bool(functions) and functions[0].get("path") == path
