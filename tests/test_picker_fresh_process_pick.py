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

from reckon import agent_context, capabilities
from reckon.agent_context import ContextRequest
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


def test_the_repository_home_wins_over_the_xdg_cache(tmp_path, monkeypatch):
    """The cache lands under RECKON_HOME, never under XDG_CACHE_HOME.

    A test isolates the repository home but has no handle on XDG_CACHE_HOME,
    which on many hosts points at the real user cache. If XDG were consulted
    first, a pick under test would write into the user's cache — and a second
    run the same day would read that entry back, calling a spy or a loader the
    test expected to run. So the order is RECKON_HOME, then XDG, and this pins
    it: the entry appears under the repository home and nothing appears under
    XDG.
    """

    reckon_home = tmp_path / "reckon-home"
    xdg_home = tmp_path / "xdg-cache"
    monkeypatch.delenv("RECKON_PICK_CACHE", raising=False)
    monkeypatch.setenv("RECKON_HOME", str(reckon_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_home))

    value = capabilities.cached_pick_input(
        "resolution", {"stamp": 1}, lambda: {"ok": True}
    )

    assert value == {"ok": True}
    assert (reckon_home / "cache" / "picker-resolution.json").exists()
    assert not xdg_home.exists()


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


def _context_fixture(tmp_path):
    home = tmp_path / "home"
    (home / ".agents").mkdir(parents=True)
    (home / ".agents" / "AGENTS.md").write_text("canonical policy\n")
    agent_root = tmp_path / "codex"
    agent_root.mkdir()
    (agent_root / "AGENTS.md").write_text("entrypoint policy\n")
    config = agent_root / "config.toml"
    config.write_text("project_doc_max_bytes = 100\n")
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "AGENTS.md").write_text("project policy\n")
    package = repo / "pkg"
    package.mkdir()
    (package / "AGENTS.md").write_text("package policy\n")
    return home, agent_root, config, repo, package


def test_cached_context_manifest_tracks_every_input_it_reads(cache_dir, tmp_path):
    """On real files the cached manifest equals the uncached one, change by change.

    The context manifest reads instruction files, the agent config and the skill
    roots; the cache trusts a stamp of those paths instead of re-reading them.
    If the stamp misses an input, the cached manifest silently diverges from a
    fresh build. This builds both on a real fixture repository and moves each
    input in turn — the agent config included — asserting the two stay equal.
    """

    home, agent_root, config, repo, package = _context_fixture(tmp_path)

    def both():
        request = ContextRequest(
            target=package, user_home=home, agent="codex", agent_root=agent_root
        )
        return agent_context.build_context_manifest(
            request
        ), agent_context.cached_context_manifest(request)

    uncached, cached = both()
    assert cached == uncached

    changes = [
        (config, "project_doc_max_bytes = 999999\n"),
        (repo / "AGENTS.md", "project policy changed\n"),
        (agent_root / "AGENTS.md", "entrypoint policy changed\n"),
        (home / ".agents" / "AGENTS.md", "canonical policy changed\n"),
        (package / "AGENTS.md", "package policy changed\n"),
    ]
    for path, text in changes:
        path.write_text(text)
        uncached, cached = both()
        assert cached == uncached, (
            f"cached manifest diverged after changing {path.name}"
        )

    # The config change is the one the earlier stamp left out: it must be seen,
    # or the cached budget would still read the original limit.
    assert cached["budget"]["limit_bytes"] == 999999

    # A new instruction file appearing in a scanned directory changes the
    # selected instruction, and must be a miss rather than a stale hit.
    (package / "AGENTS.override.md").write_text("package override policy\n")
    uncached, cached = both()
    assert cached == uncached
    assert (
        cached["instructions"]["project_chain"][-1]["selected_name"]
        == "AGENTS.override.md"
    )


def test_shared_verdict_inputs_recompute_after_run_file_edit(
    cache_dir, tmp_path, monkeypatch
):
    """An unchanged directory stamp cannot hide an edited run payload."""
    from reckon import ledger

    repo = tmp_path / "repo"
    source = ledger.run_path("sample", "record", repo)
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"run_id": "record", "gate": "passed"}))
    monkeypatch.setattr(capabilities, "load_capabilities", dict)
    seen = []

    def status(*args, **kwargs):
        value = ledger.runs("sample", repo)[0]["gate"]
        seen.append(value)
        return value

    monkeypatch.setattr(capabilities, "project_cache_status", status)
    assert routing.shared_verdict_inputs("sample", repo)["cache_status"] == "passed"
    assert routing.shared_verdict_inputs("sample", repo)["cache_status"] == "passed"
    directory_stamp = source.parent.stat().st_mtime_ns
    source.write_text(json.dumps({"run_id": "record", "gate": "failed"}))
    assert source.parent.stat().st_mtime_ns == directory_stamp
    assert routing.shared_verdict_inputs("sample", repo)["cache_status"] == "failed"
    assert seen == ["passed", "failed"]
