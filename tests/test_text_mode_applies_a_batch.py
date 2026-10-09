"""Text-mode edit_plan applies a batch of replacements in one versioned write.

A ``replacements`` list carries several ``{old_html, new_html}`` pairs, applied
in order to a working copy and written once with a single version advance.
Every pair keeps the single-replacement refusals — a non-unique match, an
overlap with a ``section[data-reckon]`` region, a structured-state change — and
a refusal names the pair's index while leaving the file and its version
untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as _mcp_module
import reckon.serve as _serve_module
from reckon._plan_html import write_state
from tests.mcp_family_reload import reload_mcp_family


@pytest.fixture()
def mounted_docs(tmp_path, monkeypatch):
    """A temp docs tree mounted as a project."""
    project = "batch-proj"
    docs = tmp_path / "repo" / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}), encoding="utf-8")

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    _serve_module._MOUNTS_FILE = mounts
    _serve_module._STATE_ROOT = state_root
    _serve_module._DISC_CACHE.clear()
    reload_mcp_family()
    return project, plans


def _write_plan(plans: Path, slug: str, body: str, *, version: int = 0) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="batch-proj">'
        f"<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{body}</main></body></html>'
    )
    path = plans / f"{slug}.html"
    path.write_text(
        write_state(
            bare,
            {
                "slug": slug,
                "title": slug.title(),
                "type": "plan",
                "status": "active",
                "version": version,
            },
        ),
        encoding="utf-8",
    )
    return path


def _edit_text(project: str, slug: str, expected_version: int, **extra):
    return _mcp_module._edit_plan_tool(
        project,
        slug,
        expected_version,
        doc_type="plan",
        mode="text",
        **extra,
    )


def _call_mcp(name: str, **arguments):
    tool = next(
        item for item in _mcp_module.mcp._tool_manager.list_tools() if item.name == name
    )
    return asyncio.run(tool.run(arguments))


# ── A batch lands as one versioned write ───────────────────────────────────


def test_a_two_pair_batch_lands_as_one_version(mounted_docs):
    project, plans = mounted_docs
    path = _write_plan(
        plans,
        "batched",
        '<p id="intro">Old prose.</p><p id="tail">Old tail.</p>',
    )

    result = _edit_text(
        project,
        "batched",
        0,
        replacements=[
            {
                "old_html": '<p id="intro">Old prose.</p>',
                "new_html": '<p id="intro">Revised <strong>intro</strong>.</p>',
            },
            {
                "old_html": '<p id="tail">Old tail.</p>',
                "new_html": '<p id="tail">Revised tail.</p>',
            },
        ],
    )

    assert result["ok"] is True, result
    assert result["new_version"] == 1
    text = path.read_text(encoding="utf-8")
    assert "Revised <strong>intro</strong>." in text
    assert "Revised tail." in text
    assert 'name="plan-version" content="1"' in text


def test_a_later_pair_sees_an_earlier_pairs_text(mounted_docs):
    """Pairs are applied in order to one working copy, not independently."""
    project, plans = mounted_docs
    path = _write_plan(plans, "ordered", '<p id="seed">seed</p>')

    result = _edit_text(
        project,
        "ordered",
        0,
        replacements=[
            {
                "old_html": '<p id="seed">seed</p>',
                "new_html": '<p id="seed">first</p><p id="added">added</p>',
            },
            {
                "old_html": '<p id="added">added</p>',
                "new_html": '<p id="added">second</p>',
            },
        ],
    )

    assert result["ok"] is True, result
    assert result["new_version"] == 1
    text = path.read_text(encoding="utf-8")
    assert '<p id="added">second</p>' in text
    assert '<p id="added">added</p>' not in text


def test_a_refused_pair_refuses_the_batch_and_leaves_the_plan_untouched(
    mounted_docs,
):
    project, plans = mounted_docs
    path = _write_plan(
        plans,
        "refused",
        '<p id="intro">Old.</p><p class="dup">same</p><p class="dup">same</p>',
    )
    before = path.read_bytes()

    result = _edit_text(
        project,
        "refused",
        0,
        replacements=[
            {
                "old_html": '<p id="intro">Old.</p>',
                "new_html": '<p id="intro">New.</p>',
            },
            {
                "old_html": '<p class="dup">same</p>',
                "new_html": '<p class="dup">changed</p>',
            },
        ],
    )

    assert result["ok"] is False
    assert result["error"] == "text_edit_error"
    assert "replacements[1].old_html" in result["detail"]
    assert "found 2 occurrences" in result["detail"]
    assert path.read_bytes() == before
    after = path.read_text(encoding="utf-8")
    assert '<p id="intro">New.</p>' not in after
    assert 'name="plan-version" content="0"' in after


def test_a_pair_changing_structured_state_refuses_the_batch(mounted_docs):
    project, plans = mounted_docs
    path = _write_plan(plans, "overlap", '<p id="intro">Old.</p>')
    before = path.read_bytes()

    result = _edit_text(
        project,
        "overlap",
        0,
        replacements=[
            {
                "old_html": '<p id="intro">Old.</p>',
                "new_html": '<p id="intro">New.</p>',
            },
            {
                "old_html": 'name="plan-status" content="active"',
                "new_html": 'name="plan-status" content="shipped"',
            },
        ],
    )

    assert result["ok"] is False
    assert "replacements[1].old_html" in result["detail"]
    assert "structured plan state" in result["detail"]
    assert '<p id="intro">New.</p>' not in path.read_text(encoding="utf-8")
    assert path.read_bytes() == before


# ── The single-pair form behaves as it does today ──────────────────────────


def test_the_single_pair_form_lands_and_keeps_its_unindexed_refusal(mounted_docs):
    project, plans = mounted_docs
    path = _write_plan(plans, "single", '<p id="one">one</p>')

    landed = _edit_text(
        project,
        "single",
        0,
        old_html='<p id="one">one</p>',
        new_html='<p id="one">two</p>',
    )
    assert landed["ok"] is True, landed
    assert landed["new_version"] == 1
    assert '<p id="one">two</p>' in path.read_text(encoding="utf-8")

    dup = _write_plan(plans, "single-dup", "<p>same</p><p>same</p>")
    refused = _edit_text(
        project,
        "single-dup",
        0,
        old_html="<p>same</p>",
        new_html="<p>other</p>",
    )
    assert refused["ok"] is False
    assert "old_html must match exactly once; found 2 occurrences" in refused["detail"]
    assert "replacements[0]" not in refused["detail"]
    assert "<p>same</p><p>same</p>" in dup.read_text(encoding="utf-8")


def test_an_empty_batch_is_refused(mounted_docs):
    project, plans = mounted_docs
    path = _write_plan(plans, "empty", "<p>prose</p>")
    before = path.read_bytes()

    result = _edit_text(project, "empty", 0, replacements=[])

    assert result["ok"] is False
    assert result["error"] == "text_edit_error"
    assert "non-empty" in result["detail"]
    assert path.read_bytes() == before


def test_a_pair_and_a_batch_cannot_be_mixed(mounted_docs):
    project, plans = mounted_docs
    _write_plan(plans, "mixed", "<p>prose</p>")

    result = _edit_text(
        project,
        "mixed",
        0,
        old_html="<p>prose</p>",
        new_html="<p>other</p>",
        replacements=[{"old_html": "<p>prose</p>", "new_html": "<p>other</p>"}],
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_edit_request"
    assert "not both" in result["detail"]


def test_a_malformed_batch_entry_is_refused_naming_its_index(mounted_docs):
    project, plans = mounted_docs
    path = _write_plan(plans, "malformed", "<p>prose</p>")
    before = path.read_bytes()

    with pytest.raises(ValueError, match=r"replacements\[1\] is missing 'new_html'"):
        _store_module.replace_plan_text_batch(
            project,
            "malformed",
            [
                {"old_html": "<p>prose</p>", "new_html": "<p>other</p>"},
                {"old_html": "<p>prose</p>"},
            ],
            expected_version=0,
        )

    assert path.read_bytes() == before


def test_the_registered_tool_accepts_a_serialised_batch(mounted_docs):
    """A client may deliver the replacements list as JSON text."""
    project, plans = mounted_docs
    path = _write_plan(plans, "serialised", '<p id="a">a</p><p id="b">b</p>')

    result = _call_mcp(
        "edit_plan",
        project=project,
        slug="serialised",
        expected_version=0,
        doc_type="plan",
        mode="text",
        replacements=json.dumps(
            [
                {"old_html": '<p id="a">a</p>', "new_html": '<p id="a">A</p>'},
                {"old_html": '<p id="b">b</p>', "new_html": '<p id="b">B</p>'},
            ]
        ),
    )

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    assert '<p id="a">A</p>' in text
    assert '<p id="b">B</p>' in text
