"""A decision recommendation the lead accepts, and a section-scoped decision checked.

A decision may carry a recommendation (``recommended`` plus ``recommended_by``)
that never becomes the choice by itself: an ``accept`` op is the one action
that turns it into one, recording when and by whom. A decision scoped to a
section its plan does not declare is refused at the write boundary and
reported by the roadmap, and a plan persisted blocked whose only open decision
is section-scoped keeps both blocked readings agreeing.

Hermetic temp docs tree per test, mirroring tests/test_edit_plan.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon.roadmap import build_roadmap
from reckon.serve import discover_plans
from tests.mcp_family_reload import reload_mcp_family


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Hermetic temp docs dir + mounts + state root. Returns (docs, state, project)."""
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

    reload_mcp_family()

    return docs_dir, state_root, project


def _seed_plan(
    docs_dir: Path,
    slug: str = "plan-a",
    *,
    status: str = "active",
    decisions: dict | None = None,
    section_declarations: dict | None = None,
) -> Path:
    from reckon._plan_html import write_state

    state: dict = {
        "slug": slug,
        "title": slug.title(),
        "status": status,
        "type": "plan",
        "version": 0,
    }
    if decisions is not None:
        state["decisions"] = decisions
    if section_declarations is not None:
        state["section_declarations"] = section_declarations
    bare = (
        '<!doctype html>\n<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    path = docs_dir / f"{slug}.html"
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def _recommending_decision() -> dict:
    return {
        "transport": {
            "title": "Which transport should carry the payload?",
            "choices": ["socket", "pipe"],
            "choice": "",
            "recommended": "socket",
            "recommended_by": "design-review",
        }
    }


def _raw_decision(project: str, slug: str, key: str) -> dict:
    response = mcp_module._read_plan(
        resource={"project": project, "type": "plan", "id": slug},
        view="raw",
    )
    return response["data"]["decisions"][key]


def _roadmap(docs_dir: Path, state_root: Path, project: str) -> dict:
    discovered = discover_plans(docs_dir, project, state_root)
    return build_roadmap(
        project,
        discovered["inventory"],
        discovered["sprints"],
        active_sprint_id=discovered["active_sprint_id"],
        project_manifest=discovered,
        review={},
        docs_dir=docs_dir,
    )


def test_read_plan_returns_the_recommendation_and_a_write_preserves_it(setup):
    docs_dir, _, project = setup
    _seed_plan(docs_dir, decisions=_recommending_decision())

    decision = _raw_decision(project, "plan-a", "transport")
    assert decision["recommended"] == "socket"
    assert decision["recommended_by"] == "design-review"
    assert decision["choice"] == ""

    written = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "summary", "value": "Recommendation must survive."}],
        0,
    )
    assert written["ok"] is True, written
    again = _raw_decision(project, "plan-a", "transport")
    assert again["recommended"] == "socket"
    assert again["recommended_by"] == "design-review"


def test_accept_sets_the_choice_from_the_recommendation(setup):
    docs_dir, _, project = setup
    _seed_plan(docs_dir, decisions=_recommending_decision())

    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "accept", "key": "transport", "by": "Simon McIntosh"}],
        0,
    )
    assert result["ok"] is True, result

    decision = _raw_decision(project, "plan-a", "transport")
    assert decision["choice"] == "socket"
    assert decision["by"] == "Simon McIntosh"
    assert decision["when"]


def test_accept_without_a_recommendation_is_refused(setup):
    docs_dir, _, project = setup
    _seed_plan(
        docs_dir,
        decisions={"transport": {"title": "Which transport?", "choice": ""}},
    )

    result = mcp_module._edit_plan(
        project, "plan-a", [{"op": "accept", "key": "transport"}], 0
    )
    assert result["ok"] is False
    assert "recommendation" in result["detail"]

    decision = _raw_decision(project, "plan-a", "transport")
    assert decision["choice"] == ""


def test_an_unanswered_recommendation_keeps_the_decision_open_and_blocking(setup):
    docs_dir, state_root, project = setup
    _seed_plan(docs_dir, decisions=_recommending_decision())

    result = _roadmap(docs_dir, state_root, project)
    row = next(row for row in result["pending_work"] if row["slug"] == "plan-a")

    assert row["readiness"] == "blocked"
    assert [blocker["id"] for blocker in row["decision_blockers"]] == ["transport"]
    assert "plan-a" not in {item["slug"] for item in result["ready_now"]}

    # Accepting it releases the plan, so the block was the open decision itself.
    written = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "accept", "key": "transport", "by": "Simon McIntosh"}],
        0,
    )
    assert written["ok"] is True, written
    released = _roadmap(docs_dir, state_root, project)
    released_row = next(r for r in released["pending_work"] if r["slug"] == "plan-a")
    assert released_row["decision_blockers"] == []
    assert released_row["readiness"] == "ready"


def test_a_decision_scoped_to_an_undeclared_section_is_refused(setup):
    docs_dir, _, project = setup
    _seed_plan(
        docs_dir,
        decisions={"transport": {"title": "Which transport?", "choice": ""}},
        section_declarations={"s1": "implementable", "s2": "implementable"},
    )

    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "decisions.transport.sections", "value": ["s9"]}],
        0,
    )
    assert result["ok"] is False, result
    detail = result["detail"]
    assert "s9" in detail
    assert "s1" in detail and "s2" in detail

    decision = _raw_decision(project, "plan-a", "transport")
    assert decision["sections"] == []

    # Positive control: the declared section writes cleanly, so the refusal is
    # about this scope and not about section scopes in general.
    accepted = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "decisions.transport.sections", "value": ["s1"]}],
        0,
    )
    assert accepted["ok"] is True, accepted
    assert _raw_decision(project, "plan-a", "transport")["sections"] == ["s1"]


def test_roadmap_reports_a_decision_scoped_to_an_undeclared_section(setup):
    docs_dir, state_root, project = setup
    _seed_plan(
        docs_dir,
        decisions={
            "transport": {
                "title": "Which transport?",
                "choice": "",
                "sections": ["s9"],
            }
        },
        section_declarations={"s1": "implementable"},
    )

    result = _roadmap(docs_dir, state_root, project)
    findings = [
        row
        for row in result["wiring_findings"]
        if row["code"] == "missing-decision-section"
    ]
    assert findings[0]["slug"] == "plan-a"
    assert findings[0]["extra"]["section"] == "s9"
    assert findings[0]["extra"]["decision"] == "transport"

    # The declared section reports its scope rather than a finding.
    _seed_plan(
        docs_dir,
        "plan-b",
        decisions={
            "transport": {"title": "Which transport?", "choice": "", "sections": ["s1"]}
        },
        section_declarations={"s1": "implementable"},
    )
    clean = _roadmap(docs_dir, state_root, project)
    assert [
        row.get("slug")
        for row in clean["wiring_findings"]
        if row["code"] == "missing-decision-section"
    ] == ["plan-a"]


def test_both_blocked_readings_agree_for_a_scoped_decision_only(setup):
    docs_dir, state_root, project = setup
    _seed_plan(
        docs_dir,
        "plan-scoped",
        status="blocked",
        decisions={
            "transport": {"title": "Which transport?", "choice": "", "sections": ["s1"]}
        },
        section_declarations={"s1": "implementable"},
    )
    _seed_plan(
        docs_dir,
        "plan-whole",
        status="blocked",
        decisions={"transport": {"title": "Which transport?", "choice": ""}},
        section_declarations={"s1": "implementable"},
    )

    result = _roadmap(docs_dir, state_root, project)
    rows = {row["slug"]: row for row in result["pending_work"]}

    # A scoped decision cannot be the explanation for a persisted plan-level
    # block, so the persisted fallback fires and is_blocked reads the same
    # list: the row keeps a visible blocker instead of reading deferred.
    scoped = rows["plan-scoped"]
    assert scoped["explicit_blockers"] == [{"kind": "persisted", "id": "unrecorded"}]
    assert scoped["readiness"] == "blocked"
    assert scoped["slug"] in {row["slug"] for row in result["blocked"]}

    # An unscoped open decision already explains the persisted block, so the
    # fallback stays out of the way.
    whole = rows["plan-whole"]
    assert whole["explicit_blockers"] == []
    assert whole["readiness"] == "blocked"
