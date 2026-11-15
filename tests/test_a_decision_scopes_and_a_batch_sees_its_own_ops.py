"""A decision holds only its sections, and a batch sees its own earlier ops.

An open decision that names the sections it holds releases the rest of the
plan; only the sections it names wait on it. And the ops of one edit batch are
applied in order against one working state, so a close batched behind the
terminal evidence that satisfies it lands in a single write rather than being
refused because the evidence had not reached disk yet.

Both cases are driven through the edit tool against a temporary project and
read back from the plan HTML and the roadmap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from reckon._schema import decision_sections
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
    followups: list | None = None,
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
    if followups is not None:
        state["followups"] = followups
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


def _row(roadmap: dict, slug: str) -> dict:
    return next(row for row in roadmap["pending_work"] if row["slug"] == slug)


def test_a_decision_scoped_by_an_edit_holds_only_the_section_it_names(setup):
    docs_dir, state_root, project = setup
    plan_path = _seed_plan(
        docs_dir,
        decisions={
            "transport": {
                "title": "Which transport carries the payload?",
                "choices": ["socket", "pipe"],
                "choice": "",
            }
        },
        section_declarations={"s1": "implementable", "s2": "implementable"},
    )

    # An unscoped open decision holds the whole plan.
    before = _row(_roadmap(docs_dir, state_root, project), "plan-a")
    assert before["readiness"] == "blocked"
    assert [blocker["id"] for blocker in before["decision_blockers"]] == ["transport"]

    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "set", "path": "decisions.transport.sections", "value": ["s2"]}],
        0,
    )
    assert result["ok"] is True, result

    # The scope is on the plan's own decision element, not only in the write.
    assert decision_sections(plan_path.read_text(encoding="utf-8")) == {
        "transport": ["s2"]
    }

    after = _row(_roadmap(docs_dir, state_root, project), "plan-a")
    assert after["blocked_sections"] == ["s2"]
    assert "s1" in after["ready_sections"]


def _terminal_evidence(plan_slug: str) -> dict:
    return {
        "op": "append_evidence",
        "plan": plan_slug,
        "anchor": "scoped-close-run",
        "title": "Run this close's evidence names",
        "body": "<p>The run delivered the plan's last section.</p>",
    }


def _close() -> dict:
    return {
        "op": "close",
        "status": "archived",
        "outcome": "done — no followup; the plan's work is recorded here",
        "by": "reckon-build",
    }


def test_a_close_batched_behind_its_evidence_lands_in_one_write(setup):
    docs_dir, _state_root, project = setup
    _seed_plan(
        docs_dir,
        status="active",
        followups=[
            {
                "id": "driver",
                "title": "land the plan",
                "body": "why",
                "written_by": "reckon-build",
                "written_at": "2026-08-12",
                "prompt": "/reckon-build plan-a §2",
            }
        ],
        section_declarations={"s1": "done", "s2": "done"},
    )

    result = mcp_module._edit_plan(
        project,
        "plan-a",
        [_terminal_evidence("plan-a"), _close()],
        0,
    )

    assert result["ok"] is True, result
    state, _version = _store_module.read_plan(project, "plan-a")
    assert state["status"] == "archived"
    assert [item["status"] for item in state["followups"]] == ["resolved"]
    record = (
        docs_dir / "evidence" / "archive" / "plan-a-landed.html"
        if (docs_dir / "evidence" / "archive" / "plan-a-landed.html").is_file()
        else docs_dir / "evidence" / "plan-a-landed.html"
    )
    assert "scoped-close-run" in record.read_text(encoding="utf-8")
