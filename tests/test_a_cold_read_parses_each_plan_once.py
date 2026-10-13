"""A cold read builds each plan file's soup once, whichever reader asks.

Three readers build a BeautifulSoup of a plan file: the memoised
``read_state_and_text_file``, ``parse_plan`` (through ``_parse_plan_uncached``)
and project state's ``_cached_plan_state``. A cold roadmap read and a cold
discovery read must each build every plan file's soup exactly once, and each
reader must return what a direct parse of the file's bytes produces.

The corpus is a distributed project whose single sprint lists every plan, so
both reads engage all three readers: readiness parses each plan, the project
state hydrates the sprint items from the same files, and the roadmap's wiring
reads them again.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

import reckon.serve as serve_module
from reckon import _plan_html, file_memo, metadata_index, project_state
from reckon._plan_html import write_state

PROJECT = "sample"
PLANS = 10


@pytest.fixture()
def soups(monkeypatch) -> Counter:
    """Count every soup built, keyed by the text it was built from."""

    counts: Counter = Counter()
    original = _plan_html.BeautifulSoup

    def counting(markup="", *args, **kwargs):
        counts[str(markup)] += 1
        return original(markup, *args, **kwargs)

    monkeypatch.setattr(_plan_html, "BeautifulSoup", counting)
    return counts


def _clear_caches() -> None:
    serve_module._DISC_CACHE.clear()
    serve_module._SIGNATURE_MEMO.clear()
    metadata_index.clear()
    file_memo.clear()
    with project_state._PLAN_STATE_CACHE_LOCK:
        project_state._PLAN_STATE_CACHE.clear()


def _sprint_document(items: list[str]) -> str:
    state = {
        "project": PROJECT,
        "id": "current",
        "type": "sprint",
        "status": "active",
        "theme": "work",
        "items": items,
        "version": 1,
    }
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="sprint">'
        '<meta name="reckon-id" content="current">'
        '<meta name="reckon-version" content="1"><title>current</title></head>'
        '<body><main class="reckon-resource" data-type="sprint" '
        'data-id="current"><ol data-reckon="sprint-items"></ol>'
        '<script type="application/json" id="reckon-resource-state">'
        f"{json.dumps(state)}</script></main></body></html>"
    )


@pytest.fixture()
def mounted(tmp_path, monkeypatch):
    """A distributed project of ten plans, its sprint listing all of them."""

    from tests.mcp_family_reload import reload_mcp_family

    docs = tmp_path / "repo" / "docs"
    (docs / "plans").mkdir(parents=True)
    (docs / ".reckon").mkdir(parents=True)
    (docs / "sprints").mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}))

    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps(
            {
                "format": "distributed",
                "status": "complete",
                "project": PROJECT,
                "resources": [],
            }
        ),
        encoding="utf-8",
    )
    items = [f"plan-{index:02d}" for index in range(PLANS)]
    (docs / "sprints" / "current.html").write_text(
        _sprint_document(items), encoding="utf-8"
    )

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setattr(serve_module, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve_module, "_STATE_ROOT", state_root)
    _clear_caches()
    mcp = reload_mcp_family()
    return docs, state_root, mcp


def _write_plan(docs: Path, slug: str) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(bare, {"slug": slug, "title": slug, "status": "active"}),
        encoding="utf-8",
    )
    return path


def _corpus(docs: Path) -> dict[str, str]:
    texts: dict[str, str] = {}
    for index in range(PLANS):
        slug = f"plan-{index:02d}"
        texts[slug] = _write_plan(docs, slug).read_text(encoding="utf-8")
    return texts


def test_a_cold_roadmap_read_builds_each_plans_soup_once(mounted, soups) -> None:
    docs, _state_root, mcp = mounted
    texts = _corpus(docs)
    soups.clear()

    mcp._roadmap(PROJECT)

    for slug, text in texts.items():
        assert soups[text] == 1, (
            f"a cold roadmap read built {slug}'s soup {soups[text]} times"
        )


def test_a_cold_discovery_read_builds_each_plans_soup_once(mounted, soups) -> None:
    docs, state_root, _mcp = mounted
    texts = _corpus(docs)
    soups.clear()

    serve_module.discover_plans(docs, PROJECT, state_root)

    for slug, text in texts.items():
        assert soups[text] == 1, (
            f"a cold discovery read built {slug}'s soup {soups[text]} times"
        )


def test_composing_the_project_state_reuses_the_shared_parse(mounted, soups) -> None:
    docs, _state_root, _mcp = mounted
    texts = _corpus(docs)
    soups.clear()

    # The shared parse reads each plan once. Composing the project state then
    # hydrates the sprint items through _plan_state_by_slug, which reaches
    # _cached_plan_state for each plan the sprint names. A cold compose is the
    # only reader in its own path, so it cannot show whether that state derives
    # from the shared parse; reading each plan first and then composing does:
    # the compose must land on the parse already built, not build a second soup.
    for slug in texts:
        _plan_html.read_state_and_text_file(docs / "plans" / f"{slug}.html")
    project_state.compose_project_state(docs, PROJECT)

    for slug, text in texts.items():
        assert soups[text] == 1, (
            f"composing the project state after a shared read built {slug}'s "
            f"soup {soups[text]} times"
        )


def test_the_three_readers_agree_with_a_direct_parse(mounted) -> None:
    docs, _state_root, _mcp = mounted
    for index in range(PLANS):
        slug = f"plan-{index:02d}"
        path = _write_plan(docs, slug)
        text = path.read_text(encoding="utf-8")
        direct = _plan_html.read_state(text)

        state, shared_text = _plan_html.read_state_and_text_file(path)
        assert state == direct
        assert shared_text == text
        assert _plan_html.read_state_file(path) == direct
        assert project_state._cached_plan_state(path) == direct

        record = _plan_html.parse_plan(path, slug)
        assert record["slug"] == slug
        assert record["status"] == direct.get("status")
        assert record["sections"] == direct.get("sections")
