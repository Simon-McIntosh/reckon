"""The sprint view carries the same wiring finding the document audit emits."""

from __future__ import annotations

from reckon.doccheck import unwired_plan_finding
from reckon.roadmap import build_roadmap

PROJECT = "unmounted-project-for-wiring-findings"
ENFORCED_FROM = "2026-09-19"


def _plan(**overrides) -> dict:
    row = {
        "slug": "plan",
        "type": "plan",
        "status": "active",
        "modified": ENFORCED_FROM,
        "depends_on": [],
        "blocks": [],
        "informs": [],
        "gates": [],
    }
    row.update(overrides)
    return row


def _wiring(report):
    return [f for f in report["wiring_findings"] if f["code"] == "unwired-plan"]


def test_roadmap_reports_an_unwired_plan_as_an_error():
    report = build_roadmap(PROJECT, [_plan(slug="unwired")], [])

    (finding,) = _wiring(report)

    assert finding["severity"] == "error"
    assert finding["slug"] == "unwired"
    assert (
        finding["message"]
        == unwired_plan_finding(
            doc_type="plan",
            status="active",
            modified=ENFORCED_FROM,
            links=[],
            gate_count=0,
            standalone=None,
            slug="unwired",
        ).message
    )


def test_roadmap_warns_on_a_plan_that_predates_the_rule():
    report = build_roadmap(PROJECT, [_plan(slug="legacy", modified="2026-08-01")], [])

    (finding,) = _wiring(report)

    assert finding["severity"] == "warn"


def test_roadmap_is_silent_for_a_wired_plan():
    report = build_roadmap(PROJECT, [_plan(slug="wired", depends_on=["other"])], [])

    assert _wiring(report) == []


def test_roadmap_is_silent_for_a_gated_plan():
    report = build_roadmap(
        PROJECT,
        [_plan(slug="gated", gates=[{"id": "g", "verdict": "pending"}])],
        [],
    )

    assert _wiring(report) == []


def test_roadmap_is_silent_for_a_terminal_plan():
    report = build_roadmap(PROJECT, [_plan(slug="done", status="shipped")], [])

    assert _wiring(report) == []
