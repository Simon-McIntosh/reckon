"""The edit_plan write that finishes a plan offers its close.

A section moves to done through the landing write (``set
section_declarations.<id>``), so that write is where a finish is seen. Its
response carries the plan's ``readiness`` after the write, read from the
roadmap's ladder, together with the plan's ``closure_blockers``, so a
coordinator ticking the last section sees ``closable`` and the close op in the
same answer.

Three cases separate the behaviour. ``the write that ticks the last section``
reads ``closable`` with the plan's ``closure_blockers`` behind it. ``the write
that leaves one implementable section`` still reads ``ready``; that is the
control. ``a write that changes no section declaration`` carries no readiness
field at all.

Hermetic fixture mirrors ``tests/test_edit_plan_sets_a_section_field.py``: a
temporary docs tree and mounts, with the store-backed MCP modules reloaded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon import _plan_html
from reckon._store_section_ops import DECLARATIONS_PATH_HEAD
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "proj"
PLAN = "finishing-plan"
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


def _make_plan(docs_dir: Path, declarations: dict[str, str]) -> Path:
    """A plan whose two authored sections carry the given declarations."""
    authored = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        "<title>Finishing plan</title></head>\n"
        '<body><main class="plan-doc">\n'
        '<h2 id="s1">&sect;1 &mdash; One</h2>\n'
        "<p>Body one.</p>\n"
        '<h2 id="s2">&sect;2 &mdash; Two</h2>\n'
        "<p>Body two.</p>\n"
        "</main></body>\n</html>\n"
    )
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": "Finishing plan",
        "status": "active",
        "standalone": "fixture plan, unwired by design",
        "version": 0,
        "section_declarations": dict(declarations),
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": _CAPABILITY,
                "attempts": 0,
                "status": declarations[section_id],
                "links": [],
            }
            for section_id in ("s1", "s2")
        ],
    }
    path = docs_dir / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


def _edit(docs_dir: Path, ops: list[dict], version: int) -> dict:
    """Apply one state-mode batch through the MCP tool, readiness included."""
    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        checkout_path=str(docs_dir.parent),
        mode="state",
        ops=ops,
    )


def test_landing_write_offers_the_close(setup):
    """Ticking the last implementable section answers closable with the close op."""
    docs_dir = setup
    _make_plan(docs_dir, {"s1": "done", "s2": "implementable"})

    result = _edit(
        docs_dir,
        [{"op": "set", "path": f"{DECLARATIONS_PATH_HEAD}.s2", "value": "done"}],
        0,
    )
    assert result["ok"] is True, result
    assert result["readiness"] == "closable", result
    blockers = result["closure_blockers"]
    assert blockers, result
    assert blockers[-1]["kind"] == "op", blockers
    assert blockers[-1]["ops"] == ["close"], blockers


def test_working_write_still_reads_ready(setup):
    """A write leaving one implementable section reads ready, the control."""
    docs_dir = setup
    _make_plan(docs_dir, {"s1": "done", "s2": "implementable"})

    result = _edit(
        docs_dir,
        [
            {
                "op": "set",
                "path": f"{DECLARATIONS_PATH_HEAD}.s1",
                "value": "implementable",
            }
        ],
        0,
    )
    assert result["ok"] is True, result
    assert result["readiness"] == "ready", result


def test_unrelated_write_carries_no_readiness(setup):
    """A write that changes no declaration carries no readiness field."""
    docs_dir = setup
    _make_plan(docs_dir, {"s1": "done", "s2": "implementable"})

    result = _edit(
        docs_dir,
        [{"op": "set", "path": "sections.s1.links", "value": ["other-plan#s2"]}],
        0,
    )
    assert result["ok"] is True, result
    assert "readiness" not in result, result
    assert "closure_blockers" not in result, result


def test_a_failed_read_leaves_the_write_landed(setup, monkeypatch):
    """A roadmap read that cannot answer names why, and the write still lands."""
    docs_dir = setup
    path = _make_plan(docs_dir, {"s1": "done", "s2": "implementable"})
    before = path.read_text(encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("the roadmap walk is unavailable")

    monkeypatch.setattr(mcp_module, "_roadmap_scoped", _boom)
    result = _edit(
        docs_dir,
        [{"op": "set", "path": "section_declarations.s2", "value": "done"}],
        0,
    )

    assert result["ok"] is True, result
    assert result["readiness"] is None, result
    assert "readiness_error" in result, result
    assert "the roadmap walk is unavailable" in result["readiness_error"], result
    text = path.read_text(encoding="utf-8")
    assert text != before, "the write did not reach the plan"
    assert _plan_html.read_state(text)["section_declarations"]["s2"] == "done"
