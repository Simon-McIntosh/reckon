"""A plan waiting on the lead is carried by its decision or gate, not a followup.

A plan whose declared work is resolved but which keeps one section deferred
because a decision or a gate holds it is still waiting on someone. The
continuation guard used to accept only a followup or a chain-closing outcome,
so the coordinator's only legal write was a placeholder followup — the very
form the close op retired. The decision or gate already records what the
section waits on and who must act, so it stands as the continuation.

Each case below lands a plan through ``edit_plan`` and reads back whether a
new followup was appended, because the point of the rule is that none is
needed.
"""

from __future__ import annotations

import json

import pytest

import reckon._store as _store_module
import reckon.mcp as mcp_module
from tests.mcp_family_reload import reload_mcp_family


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
    reload_mcp_family()
    return docs_dir, state_root, project


def _followup(ident: str = "driver") -> dict:
    return {
        "id": ident,
        "title": f"work {ident}",
        "body": "why",
        "written_by": "reckon-build",
        "written_at": "2026-08-12",
        "prompt": "/reckon-build waiting-plan §2",
    }


def _decision(*, choice: str, sections: list[str]) -> dict:
    return {
        "title": "Who takes the deferred section?",
        "context": "",
        "choices": ["accept"],
        "option_labels": {"accept": "accept"},
        "choice": choice,
        "recommended": "accept",
        "recommended_by": "lead",
        "rationale": "the section cannot be worked here",
        "when": "",
        "by": "",
        "sections": sections,
    }


def _gate(*, verdict: str, sections: list[str]) -> dict:
    return {
        "id": "g-hold",
        "section": sections[0],
        "gated_sections": sections[1:],
        "measure": "the upstream run reports its outcome",
        "status": "closed" if verdict else "open",
        "verdict": verdict,
        "required_evidence": "",
        "evidence": "",
    }


def _waiting_plan(docs_dir, slug: str, *, decisions: dict, gates: list) -> None:
    """Write a plan whose only remaining section is deferred under a record."""
    from reckon._plan_html import write_state

    state = {
        "version": 0,
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": "active",
        "impl": 1.0,
        "section_declarations": {"s1": "done", "s2": "deferred"},
        "decisions": decisions,
        "gates": gates,
        "followups": [_followup()],
    }
    bare = (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    (docs_dir / f"{slug}.html").write_text(write_state(bare, state), encoding="utf-8")


def _land(project: str, slug: str) -> dict:
    """Resolve the plan's last followup, which is its landing write."""
    return mcp_module._edit_plan(
        project,
        slug,
        [
            {
                "op": "resolve",
                "target": "followups",
                "id": "driver",
                "by": "reckon-build",
                "outcome": "the only section left is deferred on someone else",
            }
        ],
        0,
    )


def _followups(project: str, slug: str) -> list[dict]:
    state, _version = _store_module.read_plan(project, slug)
    return [item for item in state.get("followups") or [] if isinstance(item, dict)]


def test_a_deferred_section_under_an_open_decision_is_the_continuation(setup):
    docs_dir, _state_root, project = setup
    _waiting_plan(
        docs_dir,
        "waiting-plan",
        decisions={"who": _decision(choice="", sections=["s2"])},
        gates=[],
    )

    result = _land(project, "waiting-plan")

    assert result["ok"] is True, result
    followups = _followups(project, "waiting-plan")
    assert [item["id"] for item in followups] == ["driver"]
    assert followups[0]["status"] == "resolved"


def test_locking_the_decision_leaves_the_same_landing_without_a_continuation(setup):
    """Negative control: the choice is what makes the decision a wait."""
    docs_dir, _state_root, project = setup
    _waiting_plan(
        docs_dir,
        "locked-plan",
        decisions={"who": _decision(choice="accept", sections=["s2"])},
        gates=[],
    )

    result = _land(project, "locked-plan")

    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert result["detail"] == _store_module.CONTINUATION_REQUIRED


def test_a_deferred_section_under_an_open_gate_is_the_continuation(setup):
    docs_dir, _state_root, project = setup
    _waiting_plan(
        docs_dir,
        "gated-plan",
        decisions={},
        gates=[_gate(verdict="", sections=["s2"])],
    )

    result = _land(project, "gated-plan")

    assert result["ok"] is True, result
    assert [item["id"] for item in _followups(project, "gated-plan")] == ["driver"]


def test_a_settled_gate_leaves_the_same_landing_without_a_continuation(setup):
    """Negative control: a gate with a verdict is no longer holding anything."""
    docs_dir, _state_root, project = setup
    _waiting_plan(
        docs_dir,
        "verdict-plan",
        decisions={},
        gates=[_gate(verdict="passed", sections=["s2"])],
    )

    result = _land(project, "verdict-plan")

    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert result["detail"] == _store_module.CONTINUATION_REQUIRED


def test_a_deferred_section_no_record_names_still_owes_a_continuation(setup):
    """Negative control: deferral alone is not a continuation.

    A deferred section nothing holds is a section whose wait is recorded
    nowhere, so the plan still owes an answer about what comes next.
    """
    docs_dir, _state_root, project = setup
    _waiting_plan(docs_dir, "unheld-plan", decisions={}, gates=[])

    result = _land(project, "unheld-plan")

    assert result["ok"] is False
    assert result["detail"] == _store_module.CONTINUATION_REQUIRED
