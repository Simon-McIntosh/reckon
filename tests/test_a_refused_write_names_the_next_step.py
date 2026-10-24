"""A refused or late edit_plan write names the caller's next step.

Four refusals used to make the caller read more than the answer needed:

- a version conflict arrived with no current version and no word on which
  targets moved;
- a write whose reply was lost to a deadline landed later, and its retry read
  as a fresh conflict;
- a batch reported only its first refused op;
- a section's enumerated fields (effort, capability) were learned one refusal
  at a time.

Each case drives the change through ``edit_plan`` on a temporary project, and
each is written so that removing the change turns it red. A write to a
distributed project resource — a sprint, a milestone — answers through the same
two paths, so its conflict names the targets it addressed and its request id is
idempotent as well. The node's declared negative control is the reused request
id: reusing one id for a write with different ops must be refused as a reused
id, never answered with the earlier write's version.

Hermetic fixture mirrors ``tests/test_edit_plan_sets_a_section_field.py``: a
temporary docs tree and mounts, with the store-backed MCP modules reloaded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "proj"
PLAN = "refusal-plan"
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


def _make_plan(docs_dir: Path) -> Path:
    authored = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        "<title>Refusal plan</title></head>\n"
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
        "title": "Refusal plan",
        "status": "active",
        "standalone": "fixture plan, unwired by design",
        "version": 0,
        "section_declarations": {"s1": "implementable", "s2": "implementable"},
        "sections": [
            {
                "id": section_id,
                "effort_hours": 1.0,
                "capability": _CAPABILITY,
                "attempts": 0,
                "status": "implementable",
                "links": [],
            }
            for section_id in ("s1", "s2")
        ],
    }
    path = docs_dir / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


def _edit(docs_dir: Path, ops: list[dict], version: int, request_id: str | None = None):
    import reckon.mcp as mcp_module

    return mcp_module._edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        checkout_path=str(docs_dir.parent),
        mode="state",
        ops=ops,
        request_id=request_id,
    )


def _version(docs_dir: Path) -> int:
    text = (docs_dir / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    return int(_plan_html.read_state(text).get("version", 0))


# ── 1. a version conflict names the current version and the targets ──────────


def test_conflict_carries_current_version_and_targets(setup):
    docs_dir = setup
    _make_plan(docs_dir)
    # An intervening write moves the document to version 1.
    landed = _edit(
        docs_dir, [{"op": "set", "path": "sections.s2.effort_hours", "value": 2.0}], 0
    )
    assert landed["ok"] is True, landed
    assert _version(docs_dir) == 1

    # The caller retries on the version it read before that write.
    conflict = _edit(
        docs_dir, [{"op": "set", "path": "sections.s1.effort_hours", "value": 3.0}], 0
    )
    assert conflict["ok"] is False
    assert conflict["error"] == "version_conflict"
    # Without the current version the caller cannot know what to re-read. The
    # expectation is the version the store holds now, read back off disk: a
    # value this test never passes, so echoing the caller's own 0 cannot
    # satisfy it.
    assert conflict["current_version"] == _version(docs_dir)
    assert conflict["current_version"] != conflict["expected_version"]
    # The targets the refused write addressed, each with the value it holds now.
    targets = conflict["changed_targets"]
    assert {"index": 0, "path": "sections.s1.effort_hours", "current": 1.0} in targets


# ── 2. a state-mode batch reports every refused op ───────────────────────────


def test_batch_reports_every_refused_op(setup):
    docs_dir = setup
    _make_plan(docs_dir)
    ops = [
        {"op": "set", "path": "sections.s9.effort_hours", "value": 1.0},
        {"op": "set", "path": "sections.s1.effort_hours", "value": 0.3},
        {"op": "set", "path": "sections.s8.effort_hours", "value": 1.0},
    ]
    result = _edit(docs_dir, ops, 0)
    assert result["ok"] is False
    assert result["error"] == "op_error"
    refused = result["refused_ops"]
    assert {entry["index"] for entry in refused} == {0, 1, 2}
    assert all(entry["reason"] for entry in refused)
    # Reporting only the first refused op would leave two of the three unnamed.
    assert len(refused) == 3


def test_an_unknown_verb_is_refused_before_the_batch_is_read(setup):
    """A verb outside the plan grammar is refused from the argument alone.

    The batch never reaches the store, so the refusal names the verb and the
    verbs a plan does accept rather than reporting per-op refusals the store
    would have produced.
    """

    docs_dir = setup
    _make_plan(docs_dir)
    result = _edit(docs_dir, [{"op": "bogus"}], 0)

    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert "a plan does not accept the verb 'bogus'" in result["detail"]
    assert "refused_ops" not in result


# ── 3. a request id makes a retry of a late write idempotent ─────────────────


def test_request_id_returns_the_landed_version_on_retry(setup):
    docs_dir = setup
    _make_plan(docs_dir)
    ops = [{"op": "set", "path": "sections.s1.effort_hours", "value": 2.0}]
    first = _edit(docs_dir, ops, 0, request_id="req-land")
    assert first["ok"] is True, first
    landed_version = first["new_version"]
    assert _version(docs_dir) == landed_version

    # The retry carries the same id and ops but the pre-write version: the write
    # already landed, so this must answer with its version, not a conflict.
    retry = _edit(docs_dir, ops, 0, request_id="req-land")
    assert retry["ok"] is True, retry
    assert retry.get("replayed") is True
    assert retry["new_version"] == landed_version


def test_reused_request_id_with_different_ops_is_refused(setup):
    docs_dir = setup
    _make_plan(docs_dir)
    first = _edit(
        docs_dir,
        [{"op": "set", "path": "sections.s1.effort_hours", "value": 2.0}],
        0,
        request_id="req-reuse",
    )
    assert first["ok"] is True, first

    # The same id for a different write is a caller error: it must be refused,
    # never answered with the earlier write's version.
    reused = _edit(
        docs_dir,
        [{"op": "set", "path": "sections.s2.effort_hours", "value": 3.0}],
        0,
        request_id="req-reuse",
    )
    assert reused["ok"] is False
    assert reused["error"] == "reused_request_id"
    assert "new_version" not in reused


# ── 4. an enumerated-field refusal names its legal values ────────────────────


def test_effort_refusal_names_the_quarter_hour(setup):
    docs_dir = setup
    _make_plan(docs_dir)
    result = _edit(
        docs_dir, [{"op": "set", "path": "sections.s1.effort_hours", "value": 0.3}], 0
    )
    assert result["ok"] is False
    assert result["error"] == "op_error"
    # The refusal names the accepted values, not only the value it rejected: the
    # raw validation detail names 0.25 as well, so that substring alone would
    # pass with the legal-values sentence removed.
    assert "accepts a positive multiple of 0.25" in result["detail"]


def test_capability_refusal_names_legal_values_and_an_example(setup):
    docs_dir = setup
    _make_plan(docs_dir)
    bad = {
        "version": "1.0",
        "class": "general",
        "requirements": {
            "reasoning": "standard",
            "verification": "strict",
            "risk": "high",
        },
    }
    result = _edit(
        docs_dir, [{"op": "set", "path": "sections.s1.capability", "value": bad}], 0
    )
    assert result["ok"] is False
    assert result["error"] == "op_error"
    # The legal risk levels are named, and a valid example is carried.
    assert "elevated" in result["detail"] and "critical" in result["detail"]
    assert "valid example" in result["detail"]


# ── 5. a resource write answers through the same two paths ───────────────────

SPRINT = "current"


def _sprint_edit(
    docs_dir: Path,
    ops: list[dict],
    version: int,
    request_id: str | None = None,
    create: bool = False,
):
    import reckon.mcp as mcp_module

    return mcp_module._edit_plan_tool(
        PROJECT,
        SPRINT,
        expected_version=version,
        checkout_path=str(docs_dir.parent),
        mode="state",
        doc_type="sprint",
        ops=ops,
        request_id=request_id,
        create=create,
    )


def _make_sprint(docs_dir: Path) -> None:
    """A distributed project corpus holding one sprint resource."""
    from reckon import project_state

    project_state.create_project_state(docs_dir, PROJECT)
    created = _sprint_edit(
        docs_dir, [{"op": "set", "path": "title", "value": "First"}], 0, create=True
    )
    assert created["ok"] is True, created


def test_sprint_conflict_names_its_changed_targets(setup):
    from reckon import project_state

    docs_dir = setup
    _make_sprint(docs_dir)
    # An intervening write moves the resource on.
    landed = _sprint_edit(
        docs_dir, [{"op": "set", "path": "status", "value": "active"}], 1
    )
    assert landed["ok"] is True, landed

    stored, stored_version = project_state.read_resource(
        docs_dir, PROJECT, "sprint", SPRINT
    )
    # The caller retries on the version it read before that write.
    conflict = _sprint_edit(
        docs_dir, [{"op": "set", "path": "title", "value": "Second"}], 0
    )
    assert conflict["ok"] is False, conflict
    assert conflict["error"] == "version_conflict"
    assert conflict["current_version"] == stored_version
    # The addressed target with the value the store holds now — a value neither
    # the caller nor this test supplied.
    assert {"index": 0, "path": "title", "current": stored["title"]} in (
        conflict["changed_targets"]
    )


def test_sprint_request_id_returns_the_landed_version_on_retry(setup):
    from reckon import project_state

    docs_dir = setup
    _make_sprint(docs_dir)
    _, version = project_state.read_resource(docs_dir, PROJECT, "sprint", SPRINT)
    ops = [{"op": "set", "path": "status", "value": "active"}]
    first = _sprint_edit(docs_dir, ops, version, request_id="req-sprint")
    assert first["ok"] is True, first
    landed_version = first["new_version"]

    # The retry carries the same id and ops but the pre-write version: the write
    # already landed, so this must answer with its version, not a conflict.
    retry = _sprint_edit(docs_dir, ops, version, request_id="req-sprint")
    assert retry["ok"] is True, retry
    assert retry.get("replayed") is True
    assert retry["new_version"] == landed_version
