"""The picker input cache: fresh-process hits, staleness and equivalence.

A dispatch pick is a fresh CLI process, so every input it reads is recomputed
from cold even though the files those inputs read rarely change. The cache
lets the second process skip a pure function of files, while a changed file, a
corrupt entry or a version bump stays a miss. These tests pin each edge: the
cached value equals the uncached one on the same fixture, a moved file stamp
recomputes, a corrupt entry recomputes, and a real second process skips the
build entirely.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from reckon import capabilities
from reckon.crew import routing


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    target = tmp_path / "pick-cache"
    monkeypatch.setenv("RECKON_PICK_CACHE", str(target))
    return target


def test_cached_input_equals_the_uncached_build(cache_dir):
    """The cached value is the value the uncached build produces."""

    calls = []

    def build():
        calls.append(1)
        return {"a": 1, "b": [2, 3]}

    first = capabilities.cached_pick_input("equivalence", {"stamp": 1}, build)
    second = capabilities.cached_pick_input("equivalence", {"stamp": 1}, build)

    assert first == {"a": 1, "b": [2, 3]}
    assert second == first
    assert len(calls) == 1


def test_a_changed_stamp_recomputes(cache_dir):
    """A stamp that moves from a changed file is a miss, not a stale hit."""

    calls = []

    def build():
        calls.append(1)
        return {"value": len(calls)}

    capabilities.cached_pick_input("stale", {"mtime": 1, "size": 10}, build)
    capabilities.cached_pick_input("stale", {"mtime": 2, "size": 10}, build)

    assert len(calls) == 2


def test_corrupt_cache_is_a_miss(cache_dir):
    """A corrupt cache file rebuilds the input rather than raising."""

    path = capabilities.pick_input_cache_path("corrupt", root=cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    value = capabilities.cached_pick_input(
        "corrupt", {"stamp": 1}, lambda: {"ok": True}
    )

    assert value == {"ok": True}


def test_cached_selection_inputs_match_the_uncached_build(cache_dir, monkeypatch):
    """The dispatcher's shared verdict inputs are identical cached and fresh.

    The cache exists to skip work, so the one thing it must never do is change
    the answer. The same fixture is built once uncached and once through the
    cached reader, and the two must agree key for key.
    """

    monkeypatch.setattr(capabilities, "load_capabilities", lambda *a, **k: {"c": 1})
    monkeypatch.setattr(capabilities, "project_cache_status", lambda *a, **k: "fresh")

    uncached = {
        "capability_cache": capabilities.load_capabilities(),
        "cache_status": capabilities.project_cache_status(
            None, "reckon", root=Path("/")
        ),
    }
    cached = routing.shared_verdict_inputs("reckon", Path("/"))

    assert cached == uncached
    # The second read is served from the cache file, not recomputed.
    script = cache_dir / "picker-verdict-inputs-reckon.json"
    assert json.loads(script.read_text())["value"] == uncached


def test_the_second_process_skips_the_cached_build(tmp_path):
    """A real second process reads the cache instead of rebuilding."""

    cache = tmp_path / "cache"
    spy = tmp_path / "spy.txt"
    driver = tmp_path / "driver.py"
    driver.write_text(
        textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path

            sys.path.insert(0, sys.argv[3])
            from reckon import capabilities

            spy = Path(sys.argv[1])

            def build():
                with spy.open("a", encoding="utf-8") as handle:
                    handle.write("built\\n")
                return {"answer": 42}

            value = capabilities.cached_pick_input("subprocess", {"stamp": 1}, build)
            assert value == {"answer": 42}
            """
        ),
        encoding="utf-8",
    )
    import os

    import reckon

    package_root = str(Path(reckon.__file__).resolve().parent.parent)
    env = {**os.environ, "RECKON_PICK_CACHE": str(cache)}
    for _ in range(2):
        subprocess.run(
            [sys.executable, str(driver), str(spy), "x", package_root],
            check=True,
            env=env,
        )

    assert spy.read_text(encoding="utf-8").splitlines() == ["built"]
