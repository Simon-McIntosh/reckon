"""A delivered report is admitted on what its own parser read.

A report is admitted when its own grammar reads a rubric item or a finding out
of it, and refused when the parse holds nothing. A pattern match beside the
parser is not the test: a code report whose only emitted lines are
``RUBRIC evidence: pass`` and a ``TREATMENT`` line matches the plan grammar's
``RUBRIC`` shape but parses, under the health grammar it is read by, to no item
and no finding — the item it names is not in the health rubric. Admitting it on
the match stores a review nobody wrote, with every rubric item absent.

The plan grammar keeps only lines naming items of the report's own rubric, so
the same rule refuses a plan report whose only ``RUBRIC`` line names an item
outside that rubric, and admits a bulleted plan report whose finding is read and
stored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, cli
from reckon.crew import plan_review, recovery, review
from tests.test_review_plan_command import project as project  # noqa: PLC0414

CONTENT_ITEMS = review.PLAN_REVIEW_ITEMS
CODE_KEY = "c0de" * 3

# The items a code report is admitted on, written out rather than read from the
# constant the report is built from: a comparison with the same constant cannot
# tell a correct parse from an echo of its own input.
HEALTH_ITEMS_WRITTEN = (
    "known_findings",
    "duplicate_capability",
    "unreached",
    "interface_growth",
    "width",
    "test_debt",
)


@pytest.fixture
def section_plan(project):
    """A two-section plan, so one authored edit uncovers exactly one unit."""
    home, repo, path = project
    text = path.read_text().replace(
        '<h2 id="delivery">Delivery</h2><p>Extend the existing mechanism.</p>',
        '<p>Shared introduction.</p><h2 id="a">Alpha</h2><p>Build alpha.</p>'
        '<h2 id="b">Beta</h2><p>Build beta.</p>',
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"a": "implementable", "b": "implementable"}
    state["sections"] = [
        {
            "id": name,
            "effort_hours": 1.0,
            "status": "implementable",
            "capability": {
                "version": "1.0",
                "class": "general",
                "requirements": {
                    "reasoning": "standard",
                    "verification": "strict",
                    "risk": "low",
                },
            },
            "attempts": 0,
            "links": [],
        }
        for name in ("a", "b")
    ]
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path, *, run_id, rubric):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": rubric,
        "local": True,
        "run_id": run_id,
    }


def _deliver(path, *, run_id, rubric, report):
    """Write a delivered plan report and sidecar, and let the store read them."""
    fields = recovery._review_dispatch_fields(
        _subject(path, run_id=run_id, rubric=rubric)
    )
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(report)
    refusals = plan_review.store_delivered_reviews("sample", "fixture")
    return sidecar, refusals


def _rubric_lines(items):
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


def _code_sidecar(report):
    """Write a delivered code report and its sidecar, and return the sidecar."""
    directory = plan_review.code_review_report_directory(project="sample", key=CODE_KEY)
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.md"
    report_path.write_text(report)
    payload = {
        "subject": "code",
        "project": "sample",
        "around": "",
        "neighbourhood_key": CODE_KEY,
        "reviewed_revision": "0" * 40,
        "named_paths": ["reckon/crew/review.py"],
        "modules": [],
        "report_path": str(report_path),
        "review_run_id": directory.name,
    }
    plan_review.write_review_sidecar(
        directory, payload=payload, name=plan_review.CODE_REVIEW_SIDECAR_NAME
    )
    return payload


def test_a_code_report_outside_the_health_rubric_is_refused(project):
    """A code report whose only RUBRIC line names a non-health item is refused.

    The item ``evidence`` is not in the health rubric, so the health grammar
    reads no rubric item and no finding out of the report: it is not a review,
    and nothing is stored for it.
    """
    report = "RUBRIC evidence: pass\nTREATMENT : none — nothing to repair\n"
    sidecar = _code_sidecar(report)
    with pytest.raises(ValueError, match="carries no RUBRIC or FINDING line"):
        plan_review.delivered_report_record(sidecar)

    refusals = plan_review.store_delivered_reviews("sample", "fixture")
    assert [entry["review_run_id"] for entry in refusals] == [sidecar["review_run_id"]]
    assert plan_review.list_code_reviews(project="sample") == []


def test_a_code_report_with_one_health_rubric_line_stores_that_item(project):
    """One health rubric line is enough for the code grammar to admit the report."""
    report = "RUBRIC width: pass\nTREATMENT: none — nothing to repair\n"
    record = plan_review.delivered_report_record(_code_sidecar(report))
    assert record["rubric_items"] == {"width": "pass"}
    assert [item for item in record["absent_items"] if item != "width"] == [
        item for item in HEALTH_ITEMS_WRITTEN if item != "width"
    ]


def test_a_bulleted_plan_report_is_admitted_and_its_finding_answerable(section_plan):
    """A plan report written in bullets is still admitted and read."""
    _, _repo, path = section_plan
    report = "".join(f"- RUBRIC {item}: pass\n" for item in CONTENT_ITEMS)
    report += (
        "- FINDING anchors_resolve docs/plans/fixture.html:4 — the anchor names "
        "no heading — WOULD_CHANGE_THE_PLAN: yes — REASON: the done-when reads on "
        "a unit that does not exist\n"
    )
    _sidecar, refusals = _deliver(
        path,
        run_id="content-bulleted",
        rubric=plan_review.CONTENT_RUBRIC,
        report=report,
    )
    assert refusals == []
    stored = plan_review.read_plan_review("sample", "fixture")
    assert stored is not None
    assert [finding["id"] for finding in stored["findings"]] == ["anchors_resolve-1"]

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            "anchors_resolve-1",
            "--acted",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["unanswered"] == []


def test_a_plan_report_naming_an_item_outside_its_rubric_is_refused(section_plan):
    """A plan report whose only RUBRIC line names another rubric's item is refused.

    ``wiring`` belongs to the content rubric, not the design rubric, so a design
    report carrying only that line parses to no item and no finding.
    """
    _, _repo, path = section_plan
    _sidecar, refusals = _deliver(
        path,
        run_id="design-outside-rubric",
        rubric=plan_review.DESIGN_RUBRIC,
        report="RUBRIC wiring: pass\n",
    )
    assert [entry["review_run_id"] for entry in refusals] == ["design-outside-rubric"]
    assert plan_review.read_plan_review("sample", "fixture") is None
    assert plan_review.list_plan_reviews("sample") == []
