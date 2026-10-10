"""One parse of a plan file serves every reader that derives from it.

The roadmap's authored-section reader, the serve readiness reader and the
metadata index's per-plan record each derive from the path-keyed memo
``read_state_and_text_file`` holds. A cold roadmap and a discovery read of a
fresh project must therefore parse each plan once, whichever reader asks
first, and a reader that follows must add no parse of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.serve as serve_module
from reckon import _plan_html, file_memo, metadata_index
from reckon._plan_html import write_state
from reckon.roadmap import build_roadmap

PROJECT = "sample"
PLANS = 10


@pytest.fixture()
def parses(monkeypatch) -> list[int]:
    """Count every entry into the plan parser for the duration of a test."""

    count = [0]
    original = _plan_html.read_state

    def counting(html_text, *args, **kwargs):
        count[0] += 1
        return original(html_text, *args, **kwargs)

    monkeypatch.setattr(_plan_html, "read_state", counting)
    return count


@pytest.fixture()
def mounted(tmp_path, monkeypatch):
    from tests.mcp_family_reload import reload_mcp_family

    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    serve_module._SIGNATURE_MEMO.clear()
    metadata_index.clear()
    file_memo.clear()
    reload_mcp_family()
    return docs, state_root


def _write_plan(docs: Path, slug: str) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
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


def _inventory() -> list[dict]:
    return [
        {"slug": f"plan-{index:02d}", "type": "plan", "status": "active"}
        for index in range(PLANS)
    ]


def test_a_cold_roadmap_and_discovery_read_parse_each_plan_once(
    mounted, parses
) -> None:
    docs, state_root = mounted
    for index in range(PLANS):
        _write_plan(docs, f"plan-{index:02d}")

    # A cold roadmap read: the authored-section reader parses each plan once.
    build_roadmap(PROJECT, _inventory(), [], docs_dir=docs)
    # A discovery read that follows derives from the same parse.
    serve_module.discover_plans(docs, PROJECT, state_root)
    # The metadata index build asks next; its per-plan record reuses it too.
    metadata_index.index_rows(docs, PROJECT)

    assert parses[0] <= PLANS, (
        f"{parses[0]} parses for {PLANS} plans: a reader re-parsed a file "
        "another reader already parsed"
    )


def test_the_index_build_can_ask_first_without_reparsing(mounted, parses) -> None:
    docs, state_root = mounted
    for index in range(PLANS):
        _write_plan(docs, f"plan-{index:02d}")

    # The metadata index build asks first, so its per-plan record is the parse
    # every later reader must reuse.
    metadata_index.index_rows(docs, PROJECT)
    build_roadmap(PROJECT, _inventory(), [], docs_dir=docs)
    serve_module.discover_plans(docs, PROJECT, state_root)

    assert parses[0] <= PLANS, (
        f"{parses[0]} parses for {PLANS} plans: a reader re-parsed a file "
        "another reader already parsed"
    )
