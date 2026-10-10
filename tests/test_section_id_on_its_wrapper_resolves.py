"""A section id on its wrapping element resolves in collapse and in the read.

Some plans carry the section id on a ``<section id=...>`` element wrapping the
heading rather than on the ``h2`` itself. Both the collapse op and the section
read locate such a section, and both keep locating one whose id sits on its h2.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.mcp as mcp_module
from reckon._plan_html import read_state, write_state
from reckon.cli import main
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "wrapper-project"
WRAPPED = "wrapped-shape"
HEADED = "h2-shape"
SUMMARY = "Built the wrapped thing; suite green."
SUMMARY_2 = "Refreshed after a second pass; the suite is still green."
ANCHOR = "/reckon/evidence/archive/wrapped-shape-landed#s1"
ANCHOR_2 = "/reckon/evidence/archive/wrapped-shape-landed#s1-refreshed"

DECLARATIONS = {"intro": "done", "s1": "implementable", "s2": "implementable"}

WRAPPED_AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-standalone" content="fixture wiring reason">'
    "<title>Wrapped shape</title></head><body>"
    '<main class="plan-doc">'
    '<h2 id="intro">Intro</h2><p>Intro prose.</p>'
    '<section id="s1" data-reckon="section">'
    "<h2>Wrapped first</h2>"
    "<p>The reckoned-wrapper phrase.</p></section>"
    '<section id="s2">'
    "<h2>Wrapped second</h2>"
    "<p>The bare-wrapper phrase.</p></section>"
    "</main></body></html>"
)

HEADED_AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-standalone" content="fixture wiring reason">'
    "<title>Headed shape</title></head><body>"
    '<main class="plan-doc">'
    '<h2 id="s1">Headed first</h2>'
    "<p>The h2-borne phrase.</p>"
    "</main></body></html>"
)

BODY_PHRASES = {
    "s1": "The reckoned-wrapper phrase.",
    "s2": "The bare-wrapper phrase.",
}


def _plan_state(slug: str) -> dict:
    return {
        "project": PROJECT,
        "type": "plan",
        "slug": slug,
        "title": "Section identity demo",
        "status": "active",
        "modified": "2026-10-02",
        "version": 0,
        "section_declarations": deepcopy(DECLARATIONS),
        "comments": {
            "s1": [
                {
                    "id": "c-one",
                    "who": "reviewer",
                    "when": "2026-10-02",
                    "body": "The wrapper comment.",
                }
            ]
        },
        "followups": [
            {"id": "next-action", "status": "open", "prompt": f"/reckon-build {slug}"}
        ],
    }


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary checkout whose docs carry both section shapes."""
    root = tmp_path / "repo"
    docs_dir = root / "docs"
    (docs_dir / "plans").mkdir(parents=True, exist_ok=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts_file
    serve_module._STATE_ROOT = state_root
    reload_mcp_family()

    for slug, authored in ((WRAPPED, WRAPPED_AUTHORED), (HEADED, HEADED_AUTHORED)):
        (docs_dir / "plans" / f"{slug}.html").write_text(
            write_state(authored, _plan_state(slug)), encoding="utf-8"
        )
    return root


def _plan_path(root: Path, slug: str) -> Path:
    return root / "docs" / "plans" / f"{slug}.html"


def _collapse_op(section: str, summary: str = SUMMARY, anchor: str = ANCHOR) -> dict:
    return {
        "op": "collapse_section",
        "section": section,
        "summary": summary,
        "evidence_anchor": anchor,
    }


def _edit(root: Path, slug: str, op: dict) -> dict:
    path = _plan_path(root, slug)
    state = read_state(path.read_text(encoding="utf-8"))
    return mcp_module._edit_plan_tool(
        PROJECT,
        slug,
        expected_version=state["version"],
        checkout_path=str(root),
        doc_type="plan",
        mode="state",
        ops=[op],
    )


def _read(section: str, slug: str = WRAPPED) -> dict:
    return mcp_module._read_plan(
        project=PROJECT, slug=slug, view="section", section=section
    )


@pytest.mark.parametrize("section_id", ["s1", "s2"])
def test_collapse_resolves_an_id_on_its_wrapper(
    checkout: Path, section_id: str
) -> None:
    path = _plan_path(checkout, WRAPPED)

    result = _edit(checkout, WRAPPED, _collapse_op(section_id))

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    # The identity survives the collapse, now carried by the heading.
    assert f'<h2 id="{section_id}">' in text
    # The wrapper element no longer carries the section.
    assert f'<section id="{section_id}"' not in text
    # The authored body is gone and the landed card is in its place.
    assert BODY_PHRASES[section_id] not in text
    assert 'class="section-landed"' in text
    assert "&#10003; landed" in text
    assert SUMMARY in text
    assert f'<a href="{ANCHOR}">full record</a>' in text
    audit = CliRunner().invoke(main, ["audit-doc", str(path)])
    assert audit.exit_code == 0, audit.output


@pytest.mark.parametrize("section_id", ["s1", "s2"])
def test_collapsing_a_wrapper_twice_leaves_one_card(
    checkout: Path, section_id: str
) -> None:
    path = _plan_path(checkout, WRAPPED)
    assert _edit(checkout, WRAPPED, _collapse_op(section_id))["ok"] is True

    result = _edit(checkout, WRAPPED, _collapse_op(section_id, SUMMARY_2, ANCHOR_2))

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    assert text.count('<section class="section-landed"') == 1
    assert SUMMARY not in text
    assert SUMMARY_2 in text
    assert f'<a href="{ANCHOR_2}">full record</a>' in text
    assert f'<h2 id="{section_id}">' in text


def test_collapse_still_resolves_an_id_on_its_h2(checkout: Path) -> None:
    path = _plan_path(checkout, HEADED)

    result = _edit(checkout, HEADED, _collapse_op("s1"))

    assert result["ok"] is True, result
    text = path.read_text(encoding="utf-8")
    assert '<h2 id="s1">Headed first</h2>' in text
    assert "The h2-borne phrase." not in text
    assert 'class="section-landed"' in text


@pytest.mark.parametrize("section_id", ["s1", "s2"])
def test_section_read_resolves_an_id_on_its_wrapper(
    checkout: Path, section_id: str
) -> None:
    result = _read(section_id)

    assert result["view"] == "section"
    section = result["section"]
    assert section["id"] == section_id
    assert "Wrapped" in section["heading"]
    assert BODY_PHRASES[section_id] in section["text"]
    # The neighbouring sections' prose stays out of the selection.
    other = "s2" if section_id == "s1" else "s1"
    assert BODY_PHRASES[other] not in section["text"]
    assert "Intro prose." not in section["text"]
    assert section["declaration"] == "implementable"
    if section_id == "s1":
        assert [comment["id"] for comment in section["comments"]] == ["c-one"]
    else:
        assert section["comments"] == []


def test_section_read_still_resolves_an_id_on_its_h2(checkout: Path) -> None:
    result = _read("s1", HEADED)

    assert result["view"] == "section"
    section = result["section"]
    assert section["id"] == "s1"
    assert section["heading"] == "Headed first"
    assert "The h2-borne phrase." in section["text"]
    assert '<h2 id="s1">' in section["html"]


def test_section_read_lists_wrapper_carried_identities(checkout: Path) -> None:
    result = _read("absent")

    assert result["error"] == "section_not_found"
    assert "absent" in result["message"]
    assert "available sections: intro, s1, s2" in result["message"]


@pytest.mark.parametrize("section_id", ["s1", "s2"])
def test_collapsed_wrapper_section_still_reads_by_its_id(
    checkout: Path, section_id: str
) -> None:
    assert _edit(checkout, WRAPPED, _collapse_op(section_id))["ok"] is True

    result = _read(section_id)

    assert result["view"] == "section"
    assert result["section"]["id"] == section_id
    assert result["section"]["declaration"] == "done"
