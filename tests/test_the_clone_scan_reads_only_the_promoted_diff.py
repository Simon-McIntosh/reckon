"""The clone scan reads the promoted diff's functions and caches the rest.

``promotion_clone_matches`` fingerprints a run's added or modified functions
against the whole ``reckon/``/``tests/`` corpus. Parsing every corpus file on
every promotion is what made it 49 percent of a promotion's wall time, so the
scan parses a changed file from its head bytes and reads every other corpus file
from a cache keyed by the file's path and content digest. The cache must not
change the reading: the bounded scan reports exactly what a full parse reports,
and a second scan re-parses only the file that changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import clones

# A function with more than a six-line window, and a copy whose only difference
# is its name (so the window that omits the ``def`` line hashes alike).
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

UNRELATED = """def tally(values):
    total = sum(values)
    count = len(values)
    mean = total / count if count else 0.0
    spread = max(values) - min(values) if values else 0
    return total, count, mean, spread
"""

UNRELATED_CHANGED = (
    UNRELATED
    + """

def extra(value):
    return value
"""
)

ORIGINAL_PATH = "reckon/_timestamps.py"
COPY_PATH = "reckon/private_copy.py"
UNRELATED_PATH = "reckon/unrelated.py"


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


def test_the_bounded_scan_matches_the_full_scan(cache_root: Path) -> None:
    """A warm scan (cache reads), which the scan of the rest uses, must report
    the same matches a cold scan (every file parsed) reports."""
    head = {ORIGINAL_PATH: ORIGINAL, COPY_PATH: COPY}
    cold = clones.clone_matches(head, changed_paths=[COPY_PATH], base_sources={})
    warm = clones.clone_matches(head, changed_paths=[COPY_PATH], base_sources={})
    assert cold, "the fixture copy must be reported as a match"
    assert warm == cold
    existing = cold[0]["existing_function"]
    assert existing["path"] == ORIGINAL_PATH
    assert cold[0]["run_function"]["path"] == COPY_PATH


def test_a_second_call_parses_only_the_changed_file(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corpus is cached by content digest, so a second scan parses only the
    file whose bytes changed and reuses every unchanged file's windows."""
    parsed = _count_parses(monkeypatch)
    head = {
        ORIGINAL_PATH: ORIGINAL,
        UNRELATED_PATH: UNRELATED,
        COPY_PATH: COPY,
    }
    clones.clone_matches(head, changed_paths=[COPY_PATH], base_sources={})
    assert set(parsed) == {ORIGINAL_PATH, UNRELATED_PATH, COPY_PATH}

    parsed.clear()
    changed_head = {**head, UNRELATED_PATH: UNRELATED_CHANGED}
    clones.clone_matches(changed_head, changed_paths=[UNRELATED_PATH], base_sources={})
    assert set(parsed) == {UNRELATED_PATH}


def test_a_corpus_file_whose_bytes_change_is_reparsed(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache entry is keyed by content digest, so new bytes are re-parsed
    rather than served from the entry the old bytes wrote."""
    head = {ORIGINAL_PATH: ORIGINAL, COPY_PATH: COPY}
    clones.clone_matches(head, changed_paths=[COPY_PATH], base_sources={})

    parsed = _count_parses(monkeypatch)
    changed = {
        ORIGINAL_PATH: ORIGINAL + "\n# a new trailing comment\n",
        COPY_PATH: COPY,
    }
    clones.clone_matches(changed, changed_paths=[COPY_PATH], base_sources={})
    assert ORIGINAL_PATH in parsed


def test_a_changed_file_outside_the_corpus_is_still_reported(cache_root: Path) -> None:
    """The bounding skips only files that are neither changed nor corpus: a
    changed file outside the prefixes is still scanned and reported."""
    head = {"tools/script.py": ORIGINAL, COPY_PATH: COPY}
    matches = clones.clone_matches(
        head, changed_paths=["tools/script.py"], base_sources={}
    )
    assert matches, "a changed file outside the corpus must still be reported"
    assert matches[0]["run_function"]["path"] == "tools/script.py"
