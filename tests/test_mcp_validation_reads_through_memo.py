"""The audit's validation loop reads each plan through the stat-keyed memo

The audit parses every plan's HTML to validate it, and it used to call
``_plan_html.from_html`` on freshly read bytes once per plan per read, so an
unchanged corpus was re-read and re-parsed on every warm audit. These tests
hold the loop to the shared file memo: an unchanged file is parsed once and
reused, a rewrite is re-parsed, and a caller mutating the returned state cannot
poison the next reader.

Hermetic fixture mirrors tests/test_mcp_audit.py.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

import reckon._plan_html as plan_html_module
import reckon._store as _store_module
import reckon.file_memo as file_memo_module
import reckon.mcp as mcp_module


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    importlib.reload(_store_module)
    importlib.reload(mcp_module)

    file_memo_module.clear()
    yield docs_dir, state_root, project
    file_memo_module.clear()


def _make_plan_html(docs_dir: Path, slug: str, state: dict) -> Path:
    from reckon._plan_html import write_state

    bare = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    html = write_state(bare, state)
    path = docs_dir / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _valid_state(slug: str, title: str | None = None) -> dict:
    return {
        "slug": slug,
        "title": title or slug.title(),
        "status": "active",
        "version": 0,
    }


def _count_from_html(monkeypatch) -> dict[str, int]:
    """Wrap ``_plan_html.from_html`` so a test can count parses."""
    counts = {"n": 0}
    real = plan_html_module.from_html

    def wrapper(text: str):
        counts["n"] += 1
        return real(text)

    monkeypatch.setattr(plan_html_module, "from_html", wrapper)
    return counts


# ── the audit loop goes through the memo ─────────────────────────────────


def test_second_audit_reuses_the_parse(setup, monkeypatch):
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))
    _make_plan_html(docs_dir, "beta", _valid_state("beta"))
    counts = _count_from_html(monkeypatch)

    first = mcp_module._audit(project)
    assert first["checked"] == 2
    assert counts["n"] == 2  # one parse per plan on the cold read

    second = mcp_module._audit(project)
    assert second["checked"] == 2
    assert counts["n"] == 2  # unchanged plans are served from the memo


def test_changed_plan_is_reparsed_by_the_audit(setup, monkeypatch):
    docs_dir, _, project = setup
    alpha = _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))
    _make_plan_html(docs_dir, "beta", _valid_state("beta"))
    counts = _count_from_html(monkeypatch)

    mcp_module._audit(project)
    assert counts["n"] == 2

    alpha.write_text(
        _make_plan_html(
            docs_dir, "alpha", _valid_state("alpha", "Alpha again")
        ).read_text(),
    )
    mcp_module._audit(project)
    assert counts["n"] == 3  # only the changed plan is re-parsed


def test_unreadable_plan_is_still_reported(setup, monkeypatch):
    """A parse failure must still surface as a violation, not be swallowed."""
    docs_dir, _, project = setup
    _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))

    def boom(text: str):
        raise ValueError("bad document")

    monkeypatch.setattr(plan_html_module, "from_html", boom)
    file_memo_module.clear()
    result = mcp_module._audit(project)
    assert any(
        "parse error" in " ".join(item.get("errors", []))
        for item in result["violations"]
    )


# ── the memoised read itself ──────────────────────────────────────────────


def test_helper_parses_unchanged_file_once(setup, monkeypatch):
    docs_dir, _, _ = setup
    path = _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))
    counts = _count_from_html(monkeypatch)

    mcp = mcp_module._audit_plan_state(path)
    mcp_again = mcp_module._audit_plan_state(path)
    assert counts["n"] == 1
    assert mcp.slug == "alpha"
    assert mcp_again.slug == "alpha"


def test_helper_reparses_after_a_size_change(setup, monkeypatch):
    docs_dir, _, _ = setup
    path = _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))
    counts = _count_from_html(monkeypatch)

    assert mcp_module._audit_plan_state(path).title == "Alpha"
    path.write_text(
        _make_plan_html(
            docs_dir, "alpha", _valid_state("alpha", "A longer title")
        ).read_text(),
    )
    assert mcp_module._audit_plan_state(path).title == "A longer title"
    assert counts["n"] == 2


def test_helper_reparses_after_an_inode_change(setup):
    docs_dir, _, _ = setup
    path = _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))

    assert mcp_module._audit_plan_state(path).title == "Alpha"
    staged = path.with_suffix(".staged")
    staged.write_text(
        _make_plan_html(
            docs_dir, "alpha", _valid_state("alpha", "Alpha again")
        ).read_text(),
    )
    os.replace(staged, path)  # same size, different inode
    assert mcp_module._audit_plan_state(path).title == "Alpha again"


def test_returned_state_is_isolated(setup):
    docs_dir, _, _ = setup
    path = _make_plan_html(docs_dir, "alpha", _valid_state("alpha"))

    first = mcp_module._audit_plan_state(path)
    first.title = "mutated"
    assert mcp_module._audit_plan_state(path).title == "Alpha"
