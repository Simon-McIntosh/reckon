"""Setting one field of an existing plan section through ``edit_plan``.

``edit_plan`` grows one ``set`` path — ``sections.<id>.<field>`` — over five
fields. The ``h2`` ``title`` and the authored ``body`` are HTML the state writer
leaves alone, so each is applied through the replacement path the text editor
uses and the write owes the review a hand replacement would. ``links``,
``effort_hours`` and ``capability`` are typed metadata the writer regenerates
from the record.
A section's ``status`` stays a declaration and is refused in this spelling,
naming ``section_declarations.<id>``.

Hermetic fixture mirrors ``tests/test_edit_plan.py``: a temporary docs tree and
mounts, with the store-backed MCP modules reloaded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.mcp as mcp_module
from reckon import _plan_html
from reckon.cli import main
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "proj"
PLAN = "section-plan"
_CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Hermetic temp docs dir + mounts + state root. Returns (docs, proj)."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    reload_mcp_family()
    return docs_dir


def _make_section_plan(docs_dir: Path) -> Path:
    """A plan whose three authored sections each carry their typed record."""
    authored = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        "<title>Section plan</title></head>\n"
        '<body><main class="plan-doc">\n'
        '<h2 id="s1">&sect;1 &mdash; One</h2>\n'
        "<p>Body one.</p>\n"
        '<h2 id="s2">&sect;2 &mdash; Two</h2>\n'
        "<p>The plan&#x27;s second body.</p>\n"
        '<h2 id="s3">&sect;3 &mdash; Three</h2>\n'
        "<p>Body three.</p>\n"
        "</main></body>\n</html>\n"
    )
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "Section plan",
        "status": "active",
        "standalone": "fixture plan, unwired by design",
        "version": 0,
        "section_declarations": {
            "s1": "implementable",
            "s2": "implementable",
            "s3": "implementable",
        },
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": _CAPABILITY,
                "attempts": 0,
                "status": "implementable",
                "links": [],
            }
            for section_id in ("s1", "s2", "s3")
        ],
    }
    path = docs_dir / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


def _edit(docs_dir: Path, ops: list[dict], version: int) -> dict:
    """Apply one state-mode batch through the MCP tool, review debt included."""
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        checkout_path=str(docs_dir.parent),
        mode="state",
        ops=ops,
    )


def _read(docs_dir: Path) -> tuple[str, dict]:
    text = (docs_dir / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    return text, _plan_html.read_state(text)


def _authored_bytes(text: str, section_id: str) -> dict[str, str]:
    """The exact authored title and body bytes of one section."""
    spans = _plan_html.section_field_spans(text, section_id)
    assert spans is not None, f"no authored heading for {section_id!r}"
    (t0, t1), (b0, b1) = spans
    return {"title": text[t0:t1], "body": text[b0:b1]}


def _audit_ok(path: Path) -> None:
    result = CliRunner().invoke(main, ["audit-doc", str(path)])
    assert result.exit_code == 0, result.output


def _record_for(state: dict, section_id: str) -> dict:
    return next(row for row in state["sections"] if row["id"] == section_id)


# ── the five fields ──────────────────────────────────────────────────────────


def test_set_title_rewrites_the_h2_and_keeps_its_id(setup):
    docs_dir = setup
    path = _make_section_plan(docs_dir)
    before, _ = _read(docs_dir)
    others = {sid: _authored_bytes(before, sid) for sid in ("s2", "s3")}

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "sections.s1.title", "value": "§1 — Renamed"}],
        0,
    )
    assert result["ok"] is True, result

    text, _ = _read(docs_dir)
    assert '<h2 id="s1">§1 — Renamed</h2>' in text
    for sid, snapshot in others.items():
        assert _authored_bytes(text, sid) == snapshot, sid
    _audit_ok(path)


def test_set_links_writes_the_typed_record(setup):
    docs_dir = setup
    path = _make_section_plan(docs_dir)

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "sections.s1.links", "value": ["other-plan#s2"]}],
        0,
    )
    assert result["ok"] is True, result

    _, state = _read(docs_dir)
    assert _record_for(state, "s1")["links"] == ["other-plan#s2"]
    _audit_ok(path)


def test_set_effort_hours_writes_the_typed_record(setup):
    docs_dir = setup
    path = _make_section_plan(docs_dir)

    result = _edit(
        docs_dir, [{"op": "set", "path": "sections.s1.effort_hours", "value": 2.25}], 0
    )
    assert result["ok"] is True, result

    _, state = _read(docs_dir)
    assert _record_for(state, "s1")["effort_hours"] == 2.25
    _audit_ok(path)


def test_set_capability_writes_the_typed_record(setup):
    docs_dir = setup
    path = _make_section_plan(docs_dir)
    capability = {
        "version": "1.0",
        "class": "general",
        "requirements": {
            "reasoning": "deep",
            "verification": "strict",
            "risk": "moderate",
        },
    }

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "sections.s1.capability", "value": capability}],
        0,
    )
    assert result["ok"] is True, result

    _, state = _read(docs_dir)
    assert _record_for(state, "s1")["capability"]["requirements"]["reasoning"] == "deep"
    _audit_ok(path)


def test_set_body_replaces_prose_holding_an_entity_apostrophe(setup):
    docs_dir = setup
    path = _make_section_plan(docs_dir)
    before, _ = _read(docs_dir)
    assert "The plan&#x27;s second body." in before
    others = {sid: _authored_bytes(before, sid) for sid in ("s1", "s3")}

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "sections.s2.body", "value": "<p>Rewritten body.</p>"}],
        0,
    )
    assert result["ok"] is True, result

    text, _ = _read(docs_dir)
    assert "Rewritten body." in text
    assert "The plan&#x27;s second body." not in text
    for sid, snapshot in others.items():
        assert _authored_bytes(text, sid) == snapshot, sid
    owed = {entry["unit"] for entry in (result["review_owed"] or [])}
    assert "s2" in owed, result
    _audit_ok(path)


# ── negative controls ────────────────────────────────────────────────────────


def test_set_section_status_is_refused_naming_the_declaration_path(setup):
    docs_dir = setup
    _make_section_plan(docs_dir)

    result = _edit(
        docs_dir, [{"op": "set", "path": "sections.s1.status", "value": "done"}], 0
    )
    assert result["ok"] is False, result
    detail = result.get("detail") or ""
    assert "section_declarations" in detail
    assert "sections.s1.status" in detail


def test_a_body_containing_a_heading_is_refused(setup):
    docs_dir = setup
    path = _make_section_plan(docs_dir)
    before = path.read_text(encoding="utf-8")

    result = _edit(
        docs_dir,
        [
            {
                "op": "set",
                "path": "sections.s1.body",
                "value": '<p>ok</p><h2 id="forged">Forged</h2>',
            }
        ],
        0,
    )
    assert result["ok"] is False, result
    assert "must not contain a heading" in (result.get("detail") or "")
    # Nothing was written: the forged heading never reached the document.
    assert path.read_text(encoding="utf-8") == before


def test_set_title_on_a_missing_section_is_refused_naming_it(setup):
    docs_dir = setup
    _make_section_plan(docs_dir)

    result = _edit(
        docs_dir, [{"op": "set", "path": "sections.s9.title", "value": "Nope"}], 0
    )
    assert result["ok"] is False, result
    assert "s9" in (result.get("detail") or "")


def test_an_unknown_field_is_refused_listing_the_accepted_five(setup):
    docs_dir = setup
    _make_section_plan(docs_dir)

    result = _edit(
        docs_dir, [{"op": "set", "path": "sections.s1.nonsense", "value": "x"}], 0
    )
    assert result["ok"] is False, result
    detail = result.get("detail") or ""
    for field in ("title", "links", "effort_hours", "capability", "body"):
        assert field in detail, (field, detail)
