"""Setting followup text, section dependencies, gate text and null removals.

``edit_plan`` grows four surfaces on the ``set`` op:

* ``followups.<id>.title`` and ``.body`` on an open followup;
* ``section_depends_on.<sid>`` with a list of ``[project:]slug#section`` refs;
* ``gates.<id>.measure`` and ``.required_evidence`` on an open gate;
* a ``null`` value, which removes one of four kinds of key.

Each surface gets its own case and its own negative control, and the null
surface gets one case per key kind. The section-dependency case reads the edge
back through the roadmap's own reader, so the consumer is proven and not only
the meta.

Hermetic fixture mirrors ``tests/test_edit_plan.py``: a temporary docs tree and
mounts, with the store-backed MCP modules reloaded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
import reckon.roadmap as roadmap_module
from reckon import _plan_html, _schema
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "proj"
PLAN = "gate-plan"
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
    """Hermetic temp docs dir + mounts + state root. Returns the docs dir."""
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


def _make_plan(docs_dir: Path, *, stale_declaration: bool = False) -> Path:
    """A plan carrying the state each surface addresses.

    ``s1`` to ``s3`` are authored headings with records. An open followup and a
    resolved one, an open gate and a passed gate, a decision with a rationale,
    and a plan ``summary`` the null surface can remove.

    ``stale_declaration`` adds ``s4``: a declaration whose heading is NOT
    authored, the shape a heading removed by an earlier prose edit leaves
    behind. It carries no record either, because a record with no heading cannot
    round-trip through the state writer.
    """
    authored = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        "<title>Gate plan</title></head>\n"
        '<body><main class="plan-doc">\n'
        '<h2 id="s1">&sect;1 &mdash; One</h2>\n'
        "<p>Body one.</p>\n"
        '<h2 id="s2">&sect;2 &mdash; Two</h2>\n'
        "<p>Body two.</p>\n"
        '<h2 id="s3">&sect;3 &mdash; Three</h2>\n'
        "<p>Body three.</p>\n"
        "</main></body>\n</html>\n"
    )
    declarations = {"s1": "implementable", "s2": "implementable", "s3": "implementable"}
    if stale_declaration:
        declarations["s4"] = "implementable"
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "Gate plan",
        "status": "active",
        "summary": "Original summary.",
        "standalone": "fixture plan, unwired by design",
        "version": 0,
        "section_declarations": dict(declarations),
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": _CAPABILITY,
                "attempts": 0,
                "status": status,
                "links": [],
            }
            for section_id, status in declarations.items()
            if section_id != "s4"
        ],
        "followups": [
            {
                "id": "c-open",
                "status": "open",
                "title": "Open followup",
                "body": "<p>Open body.</p>",
                "prompt": "/reckon-build gate-plan — open work",
            },
            {
                "id": "c-res",
                "status": "resolved",
                "title": "Resolved body",
                "body": "<p>Resolved.</p>",
                "prompt": "/reckon-build gate-plan — closed work",
                "resolved_at": "2026-01-01T00:00:00Z",
                "resolved_by": "simon",
                "outcome": "done — no followup",
            },
        ],
        "gates": [
            {
                "id": "g-open",
                "section": "s1",
                "measure": "Open measure",
                "required_evidence": "Open evidence",
            },
            {
                "id": "g-passed",
                "section": "s2",
                "measure": "Passed measure",
                "required_evidence": "Passed evidence",
                "status": "closed",
                "verdict": "passed",
            },
        ],
        "decisions": {
            "dec1": {
                "title": "Which path?",
                "context": "Some context.",
                "choices": ["a", "b"],
                "choice": "",
                "rationale": "Because.",
            }
        },
    }
    path = docs_dir / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


def _edit(docs_dir: Path, ops: list[dict], version: int) -> dict:
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


def _followup(state: dict, ident: str) -> dict:
    return next(row for row in state["followups"] if row["id"] == ident)


def _gate(state: dict, ident: str) -> dict:
    return next(row for row in state["gates"] if row["id"] == ident)


def _version(docs_dir: Path) -> int:
    return _read(docs_dir)[1]["version"]


# ── surface 1: followup text ─────────────────────────────────────────────────


def test_followup_title_and_body_change_on_an_open_followup(setup):
    docs_dir = setup
    _make_plan(docs_dir)

    result = _edit(
        docs_dir,
        [
            {"op": "set", "path": "followups.c-open.title", "value": "Widened scope"},
            {
                "op": "set",
                "path": "followups.c-open.body",
                "value": "<p>Widened body.</p>",
            },
        ],
        0,
    )
    assert result["ok"] is True, result

    text, state = _read(docs_dir)
    followup = _followup(state, "c-open")
    assert followup["title"] == "Widened scope"
    assert followup["body"] == "<p>Widened body.</p>"
    assert followup["status"] == "open"
    assert "Widened scope" in text
    # The resolved followup is untouched.
    assert _followup(state, "c-res")["title"] == "Resolved body"


def test_followup_text_on_a_resolved_followup_is_refused(setup):
    docs_dir = setup
    path = _make_plan(docs_dir)
    before = path.read_text(encoding="utf-8")

    result = _edit(
        docs_dir, [{"op": "set", "path": "followups.c-res.title", "value": "Nope"}], 0
    )
    assert result["ok"] is False, result
    assert "c-res" in (result.get("detail") or "")
    assert "resolved" in (result.get("detail") or "").lower()
    assert path.read_text(encoding="utf-8") == before


# ── surface 2: section dependencies ──────────────────────────────────────────


def test_section_depends_on_writes_the_meta_the_roadmap_reads(setup):
    docs_dir = setup
    path = _make_plan(docs_dir)
    refs = ["other-plan#s2"]

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "section_depends_on.s1", "value": refs}],
        0,
    )
    assert result["ok"] is True, result

    # The roadmap's own reader returns the edge — the consumer, not just a meta.
    mapping = roadmap_module._plan_section_deps({}, docs_dir, PROJECT, PLAN)
    assert mapping is not None
    assert mapping.get("s1") == refs
    # And the meta is on the page, read back by the schema's own reader.
    text = path.read_text(encoding="utf-8")
    assert _schema.section_depends_on(text) == {"s1": refs}


def test_a_ref_outside_the_grammar_is_refused(setup):
    docs_dir = setup
    path = _make_plan(docs_dir)
    before = path.read_text(encoding="utf-8")

    # Names no target section: the grammar is [project:]slug#section.
    result = _edit(
        docs_dir,
        [{"op": "set", "path": "section_depends_on.s1", "value": ["other-plan"]}],
        0,
    )
    assert result["ok"] is False, result
    assert "other-plan" in (result.get("detail") or "")
    assert path.read_text(encoding="utf-8") == before


# ── surface 3: gate text ─────────────────────────────────────────────────────


def test_gate_measure_and_required_evidence_change_on_an_open_gate(setup):
    docs_dir = setup
    _make_plan(docs_dir)

    result = _edit(
        docs_dir,
        [
            {"op": "set", "path": "gates.g-open.measure", "value": "Corrected measure"},
            {
                "op": "set",
                "path": "gates.g-open.required_evidence",
                "value": "Corrected evidence",
            },
        ],
        0,
    )
    assert result["ok"] is True, result

    text, state = _read(docs_dir)
    gate = _gate(state, "g-open")
    assert gate["measure"] == "Corrected measure"
    assert gate["required_evidence"] == "Corrected evidence"
    assert "Corrected measure" in text
    # The passed gate is untouched.
    assert _gate(state, "g-passed")["measure"] == "Passed measure"


def test_gate_text_on_a_passed_gate_is_refused(setup):
    docs_dir = setup
    path = _make_plan(docs_dir)
    before = path.read_text(encoding="utf-8")

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "gates.g-passed.measure", "value": "Nope"}],
        0,
    )
    assert result["ok"] is False, result
    assert "g-passed" in (result.get("detail") or "")
    assert "passed" in (result.get("detail") or "")
    assert path.read_text(encoding="utf-8") == before


# ── surface 4: null removal ──────────────────────────────────────────────────


def test_null_removes_the_section_dependency_entry(setup):
    docs_dir = setup
    path = _make_plan(docs_dir)
    _edit(
        docs_dir,
        [{"op": "set", "path": "section_depends_on.s2", "value": ["other-plan#s3"]}],
        0,
    )
    assert roadmap_module._plan_section_deps({}, docs_dir, PROJECT, PLAN) == {
        "s2": ["other-plan#s3"]
    }

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "section_depends_on.s2", "value": None}],
        _version(docs_dir),
    )
    assert result["ok"] is True, result

    text = path.read_text(encoding="utf-8")
    assert _schema.section_depends_on(text) is None
    assert "plan-section-depends-on" not in text


def test_null_removes_a_stale_declaration_whose_heading_is_gone(setup):
    docs_dir = setup
    path = _make_plan(docs_dir, stale_declaration=True)
    _, state = _read(docs_dir)
    assert "s4" in state["section_declarations"]

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "section_declarations.s4", "value": None}],
        0,
    )
    assert result["ok"] is True, result

    _, state = _read(docs_dir)
    assert "s4" not in state["section_declarations"]
    assert all(row["id"] != "s4" for row in state["sections"])
    assert path.read_text(encoding="utf-8")


def test_null_declaration_while_its_heading_exists_is_refused(setup):
    docs_dir = setup
    path = _make_plan(docs_dir)
    before = path.read_text(encoding="utf-8")

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "section_declarations.s1", "value": None}],
        0,
    )
    assert result["ok"] is False, result
    # The path is named, so the refusal is this handler's own and not the
    # coverage guard's backstop for the same state.
    assert "section_declarations.s1" in (result.get("detail") or "")
    assert path.read_text(encoding="utf-8") == before
    assert _read(docs_dir)[1]["section_declarations"]["s1"] == "implementable"


def test_null_removes_a_decision_field_but_not_its_choice(setup):
    docs_dir = setup
    _make_plan(docs_dir)

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "decisions.dec1.rationale", "value": None}],
        0,
    )
    assert result["ok"] is True, result
    _, state = _read(docs_dir)
    assert state["decisions"]["dec1"].get("rationale") in (None, "")

    refused = _edit(
        docs_dir,
        [{"op": "set", "path": "decisions.dec1.choice", "value": None}],
        _version(docs_dir),
    )
    assert refused["ok"] is False, refused
    assert "choice" in (refused.get("detail") or "")


def test_null_removes_an_optional_top_level_scalar(setup):
    docs_dir = setup
    path = _make_plan(docs_dir)

    result = _edit(docs_dir, [{"op": "set", "path": "summary", "value": None}], 0)
    assert result["ok"] is True, result

    text, state = _read(docs_dir)
    assert "plan-summary" not in text
    assert state.get("summary", "") == ""

    refused = _edit(
        docs_dir,
        [{"op": "set", "path": "title", "value": None}],
        _version(docs_dir),
    )
    assert refused["ok"] is False, refused
    assert "title" in (refused.get("detail") or "")
    assert path.read_text(encoding="utf-8")
