"""A persisted parse carries the code version that made it, and reads seed from it.

The metadata index already persists each unchanged file's parse, but the read
paths behind ``read_plan`` and ``edit_plan`` never seeded from it, so a fresh
process re-parsed every plan a read touched — one ``read_plan`` made 198 state
reads. The store also carried only the file's stat identity and digest, so a
parser change did not invalidate it, and a value recorded by older code was
served as if the current code had produced it.

The tree here is synthetic and mounted in a temporary home. Each case runs a
second simulated process by clearing the in-process memo, and counts
``_plan_html.read_state`` through a patch, so a zero is distinguishable from an
instrument that never fired: the building pass asserts its own count first.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon import _plan_html, file_memo, mcp_deadlines, metadata_index, serve

PROJECT = "sample"
PLAN_COUNT = 40


def _plan_html_text(slug: str, title: str | None = None) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{title or slug}">'
        '<meta name="plan-summary" content="one plan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0.25">'
        '<meta name="plan-version" content="1">'
        '<meta name="plan-section-declarations" content="{&quot;s1&quot;: &quot;implementable&quot;}">'
        '</head><body><main class="plan-doc">'
        '<h2 id="s1">§1 — the section</h2><p>authored prose</p>'
        "</main></body></html>"
    )


class Store:
    """One mounted project in a temporary configuration home."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docs = root / "repo" / "docs"
        self.home = root / "home"
        self.docs.mkdir(parents=True)
        self.home.mkdir(parents=True)
        (self.docs / "plans").mkdir()
        for number in range(PLAN_COUNT):
            slug = f"plan-{number:02d}"
            (self.docs / "plans" / f"{slug}.html").write_text(
                _plan_html_text(slug), encoding="utf-8"
            )
        self.mounts = self.home / "mounts.json"
        self.mounts.write_text(json.dumps({PROJECT: str(self.docs)}), encoding="utf-8")

    def plan_path(self, slug: str) -> Path:
        return self.docs / "plans" / f"{slug}.html"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    state = Store(tmp_path)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(state.mounts))
    monkeypatch.setenv("RECKON_HOME", str(state.home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("RECKON_MCP_READ_DEADLINE_SECONDS", raising=False)
    serve._resolve_paths(state.mounts)
    _forget_process()
    return state


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record each ``_plan_html.read_state`` call, the parse counter."""

    calls: list[str] = []
    original = _plan_html.read_state

    def counting(text: str) -> dict:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(_plan_html, "read_state", counting)
    return calls


def _forget_process() -> None:
    """Drop every in-process memo, as a restarted process starts empty."""

    file_memo.clear()
    metadata_index._CACHE.clear()
    serve._DISC_CACHE.clear()
    serve._SIGNATURE_MEMO.clear()


def _call_read(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


def _read_tool():
    return mcp_deadlines._deadline_tool(
        mcp_module._read_plan_tool,
        kind="read",
        request_scope=True,
        response_budget=False,
    )


def _read_one(slug: str) -> dict:
    return _call_read(_read_tool(), project=PROJECT, slug=slug, view="summary")


def _discover(store: Store) -> dict:
    return serve.discover_plans(store.docs, PROJECT, None)


def _build_store(store: Store) -> None:
    """Run the pass that parses the tree and persists what it parsed."""

    _discover(store)


def test_a_second_process_parses_no_unchanged_plan(store: Store, parsed: list[Path]):
    """A fresh process answers a one-plan read from the persisted parse."""

    _build_store(store)
    assert parsed, "positive control: the building pass must have parsed something"
    _forget_process()
    parsed.clear()

    result = _read_one("plan-07")

    assert result.get("slug") == "plan-07" or "plan-07" in json.dumps(result)
    assert parsed == [], f"{len(parsed)} plans were parsed by a seeded read"


def test_one_edited_plan_is_parsed_once(store: Store, parsed: list[Path]):
    """Only the file whose bytes moved is parsed again."""

    _build_store(store)
    _forget_process()
    target = store.plan_path("plan-11")
    target.write_text(_plan_html_text("plan-11", "renamed"), encoding="utf-8")
    parsed.clear()

    edited = _read_one("plan-11")
    untouched = _read_one("plan-03")

    assert len(parsed) == 1, f"expected one parse, saw {len(parsed)}"
    assert "renamed" in json.dumps(edited)
    assert "plan-03" in json.dumps(untouched)


def test_a_changed_parser_source_digest_invalidates_every_stored_parse(
    store: Store, parsed: list[Path], monkeypatch: pytest.MonkeyPatch
):
    """A parser change makes every stored parse absent, so each is parsed once."""

    _build_store(store)
    _forget_process()
    real = metadata_index._module_source_digest
    changed = real(_plan_html) + "-moved"

    def moved(module: object) -> str:
        return changed if module is _plan_html else real(module)

    monkeypatch.setattr(metadata_index, "_module_source_digest", moved)
    parsed.clear()

    _discover(store)
    assert len(parsed) == PLAN_COUNT, f"expected {PLAN_COUNT} parses, saw {len(parsed)}"

    parsed.clear()
    _discover(store)
    assert parsed == [], f"{len(parsed)} plans were parsed by the rebuilt store"


def test_a_corrupt_stored_entry_yields_the_parsed_value(
    store: Store, parsed: list[Path]
):
    """A store that cannot be read is parsed, not raised from."""

    _build_store(store)
    _forget_process()
    index_files = list((store.home / "cache").rglob("*.json"))
    assert index_files, "positive control: the build must have written a store"
    for path in index_files:
        path.write_text('{"schema": "truncated"', encoding="utf-8")
    parsed.clear()

    result = _read_one("plan-23")

    assert parsed, "positive control: a corrupt store must fall back to parsing"
    assert "plan-23" in json.dumps(result)
