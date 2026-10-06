"""Remaining-work readers share classification and run-comment ownership."""

from __future__ import annotations

import ast
import inspect
import json
from html import escape
from pathlib import Path

import pytest

from reckon import _schema
from reckon._plan_html import read_state
from reckon.crew import plan_review, promotion
from reckon.doccheck import _implementable_section_ids
from reckon.followup_pointers import classify_followup
from reckon.roadmap import implementable_sections


def _plan():
    declarations = {"s1": "implementable", "s2": "implementable", "s3": "done"}
    document = (
        '<html><head><meta name="plan-slug" content="sample">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-section-declarations" content="'
        + escape(json.dumps(declarations), quote=True)
        + '"></head><body>'
        '<h2 id="s2">Outstanding work</h2>'
        '<section class="section-landed"><h2 id="s1">Recorded landing</h2>'
        "<p>A node landed; this section still admits work.</p></section>"
        '<h2 id="s3">Completed work</h2>'
        '<section data-reckon="comments">'
        '<div class="r-comment" data-section="s1" data-id="'
        + plan_review.RUN_COMMENT_PREFIX
        + 'recorded"><div class="r-comment-body">Landed.</div></div>'
        "</section></body></html>"
    )
    return read_state(document)


def test_readers_keep_landed_work_until_reclassification():
    plan = _plan()
    assert plan["section_declarations"] == {
        "s1": "implementable",
        "s2": "implementable",
        "s3": "done",
    }
    assert promotion._landed_sections(plan) == {"s1"}
    assert implementable_sections(plan["section_declarations"]) == ["s1", "s2"]
    assert _schema.plan_executable_remainder(plan) == 2
    assert _implementable_section_ids(plan, ["s2", "s1", "s3"]) == ["s2", "s1"]
    assert promotion._plan_remaining_sections(plan) == ["s2"]
    for section in ("s1", "s2", "s3"):
        verdict = classify_followup(
            plan, {"prompt": f"/reckon-build sample §{section[1:]}"}
        )
        assert verdict.reason == (
            "section-not-implementable" if section == "s3" else "implementable-section"
        )
        assert verdict.pointer is (section != "s3")

    plan["section_declarations"]["s1"] = "done"
    assert implementable_sections(plan["section_declarations"]) == ["s2"]
    assert _schema.plan_executable_remainder(plan) == 1
    assert _implementable_section_ids(plan, ["s2", "s1", "s3"]) == ["s2"]
    assert classify_followup(plan, {"prompt": "/reckon-build sample §1"}).reason == (
        "section-not-implementable"
    )


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("implementable", True),
        (" implementable ", True),
        ("done", False),
        ("deferred", False),
        ("IMPLEMENTABLE", False),
        (None, False),
        ("", False),
        (False, False),
        (1, False),
    ],
)
def test_classification_normalizes_only_surrounding_whitespace(
    classification, expected
):
    assert _schema.is_implementable_section(classification) is expected


def test_reader_fallbacks_keep_their_distinct_questions():
    plan = _plan()
    del plan["section_declarations"]
    assert implementable_sections(None) == []
    assert _schema.plan_executable_remainder(plan) is None
    assert _implementable_section_ids(plan, ["s2", "s1", "decisions"]) == ["s2", "s1"]
    assert promotion._plan_remaining_sections(plan) == []
    plan["comments"]["s2"] = [{"id": "authored", "body": "A question."}]
    assert promotion._plan_remaining_sections(plan) == ["s2"]


def test_undeclared_plan_combines_heading_and_anchor_fallbacks():
    plan = {
        "slug": "undeclared",
        "status": "active",
        "gates": [{"section": "s7", "gated_sections": ["s8"]}],
        "comments": {
            "s9": [{"id": plan_review.RUN_COMMENT_PREFIX + "landed"}],
            "s4": [{"id": "authored", "body": "A question."}],
            "_top": [{"id": "document-note"}],
        },
    }
    headings = ["s9", "decisions", "s4"]
    assert "section_declarations" not in plan
    assert _schema.plan_section_anchors(plan) == {"s4", "s7", "s8", "s9"}
    assert promotion._landed_sections(plan) == {"s9"}
    assert _implementable_section_ids(plan, headings) == ["s9", "s4"]
    assert promotion._plan_remaining_sections(plan) == ["s4", "s7", "s8"]
    assert implementable_sections(plan.get("section_declarations")) == []
    assert _schema.plan_executable_remainder(plan) is None
    for section in ("s4", "s7", "s8", "s9"):
        verdict = classify_followup(
            plan, {"prompt": f"/reckon-build undeclared §{section[1:]}"}
        )
        assert verdict.reason == "section-not-implementable"
        assert verdict.pointer is False

    plan["comments"]["s4"].append(
        {"id": plan_review.RUN_COMMENT_PREFIX + "another-landing"}
    )
    assert promotion._landed_sections(plan) == {"s4", "s9"}
    assert promotion._plan_remaining_sections(plan) == ["s7", "s8"]
    assert _implementable_section_ids(plan, headings) == ["s9", "s4"]
    assert implementable_sections(plan.get("section_declarations")) == []
    assert _schema.plan_executable_remainder(plan) is None
    assert (
        classify_followup(plan, {"prompt": "/reckon-build undeclared §4"}).reason
        == "section-not-implementable"
    )


@pytest.mark.parametrize(
    ("comment_id", "expected"),
    [
        ("c-run-example", True),
        ("c-run-", True),
        ("c-run", False),
        ("authored-c-run-example", False),
        (" c-run-example", False),
        ("C-RUN-example", False),
        (None, False),
        ("", False),
        (42, False),
    ],
)
def test_comment_filters_share_the_run_identity_rule(comment_id, expected):
    entry = {"id": comment_id}
    authored = {"id": "authored", "body": "Keep this."}
    state = {"comments": {"section": [entry, authored, "unstructured"]}}
    assert plan_review._is_run_comment(comment_id) is expected
    assert promotion._landed_sections(state) == ({"section"} if expected else set())
    assert state["comments"]["section"] == [entry, authored, "unstructured"]


def test_run_comment_prefix_has_one_owner():
    package = Path(_schema.__file__).resolve().parent
    owners = {
        str(path.relative_to(package)): path.read_text().count(
            plan_review.RUN_COMMENT_PREFIX
        )
        for path in package.rglob("*.py")
        if plan_review.RUN_COMMENT_PREFIX in path.read_text()
    }
    assert owners == {"crew/plan_review.py": 1}


def test_all_remaining_work_readers_call_the_schema_predicate():
    readers = (
        implementable_sections,
        _schema.plan_executable_remainder,
        _implementable_section_ids,
        promotion._plan_remaining_sections,
        classify_followup,
    )
    for reader in readers:
        calls = {
            node.func.id
            for node in ast.walk(ast.parse(inspect.getsource(reader)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "is_implementable_section" in calls, reader.__name__
    imports = {
        node.module
        for node in ast.walk(ast.parse(inspect.getsource(_schema)))
        if isinstance(node, ast.ImportFrom)
    }
    assert not imports.intersection(
        {
            "reckon.roadmap",
            "reckon.doccheck",
            "reckon.crew.promotion",
            "reckon.followup_pointers",
        }
    )
