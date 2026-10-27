"""A section can wait on one followup of another plan.

A coordinator's section really waited on ``f-tfcv-007`` and could only declare
the whole section that followup belonged to, so the wait outlived the followup
and the section stayed blocked behind a section that was never going to be
worked. The ref grammar carries ``slug@f-id``: one section waits on one
followup of another plan, satisfied when that followup resolves rather than
when a whole section lands.

A ref that names a followup the target plan does not carry is a
``dangling-hard-dependency``: a wait nothing can ever satisfy must be reported,
and the waiting section stays held while it is reported.
"""

from __future__ import annotations

from reckon._schema import parse_plan_ref
from reckon.roadmap import build_roadmap

PROJECT = "sample"
SPRINT = {"id": "S1", "status": "active", "items": ["x", "y"]}


def _plan(
    slug: str,
    *,
    sections: dict[str, str] | None = None,
    section_depends_on: dict[str, list[str]] | None = None,
    followups: list[dict] | None = None,
) -> dict:
    row = {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": "active",
        "impl": 0.0,
        "depends_on": [],
        "sprint": "S1",
        "effort": "M",
        "roi": "high",
        "blocking": [],
        "gates": [],
        "followups": followups or [],
        "section_declarations": sections or {"s1": "implementable"},
        "sections": [],
    }
    if section_depends_on is not None:
        row["section_depends_on"] = section_depends_on
    return row


def _open_followup(followup_id: str = "f-1") -> dict:
    return {"id": followup_id, "status": "open", "prompt": "/reckon-edit y"}


def _resolved_followup(followup_id: str = "f-1") -> dict:
    return {
        "id": followup_id,
        "status": "resolved",
        "prompt": "/reckon-edit y",
        "resolved_at": "2026-10-20T00:00:00Z",
        "resolved_by": "worker",
        "outcome": "the followup's work landed",
    }


def _report(target_followups: list[dict]) -> dict:
    plans = [
        _plan("x", section_depends_on={"s1": ["y@f-1"]}),
        _plan("y", sections={"s2": "done"}, followups=target_followups),
    ]
    return build_roadmap(PROJECT, plans, [SPRINT])


def _row(report: dict, slug: str) -> dict:
    for bucket in ("ready_now", "blocked", "deferred"):
        for row in report.get(bucket, []):
            if row["slug"] == slug:
                return row
    raise AssertionError(f"{slug} is in no roadmap bucket: {report.keys()}")


def _verdict(report: dict, slug: str, section: str = "s1") -> dict:
    readiness = _row(report, slug).get("section_readiness") or []
    for item in readiness:
        if item["section"] == section:
            return item
    raise AssertionError(f"{slug} carries no readiness row for {section}: {readiness}")


def _dangling(report: dict) -> list[dict]:
    return [
        finding
        for finding in report["wiring_findings"]
        if finding.get("code") == "dangling-hard-dependency"
    ]


# ── the grammar ──────────────────────────────────────────────────────────────


def test_the_ref_grammar_parses_a_followup_suffix():
    parsed = parse_plan_ref("y@f-1")

    assert parsed is not None
    assert parsed.slug == "y"
    assert parsed.followup == "f-1"
    assert parsed.stage is None


def test_a_section_anchor_and_a_followup_are_distinct():
    parsed = parse_plan_ref("project:y#s2@f-7")

    assert parsed is not None
    assert (parsed.project, parsed.slug, parsed.stage, parsed.followup) == (
        "project",
        "y",
        "s2",
        "f-7",
    )


# ── the wait ─────────────────────────────────────────────────────────────────


def test_a_section_is_blocked_while_its_followup_is_open():
    report = _report([_open_followup()])

    verdict = _verdict(report, "x")
    assert verdict["ready"] is False
    assert "s1" in _row(report, "x")["blocked_sections"]


def test_the_section_is_ready_once_the_followup_resolves():
    report = _report([_resolved_followup()])

    verdict = _verdict(report, "x")
    assert verdict["ready"] is True
    assert "s1" in (_row(report, "x").get("ready_sections") or [])


def test_a_section_wait_never_holds_its_whole_plan():
    report = _report([_open_followup()])

    # The wait runs between sections, so the plan stays dispatchable and only
    # the waiting section is held.
    assert _row(report, "x")["slug"] == "x"
    assert _verdict(report, "x")["ready"] is False


# ── the dangling ref ────────────────────────────────────────────────────────


def test_a_ref_to_a_followup_that_does_not_exist_is_dangling():
    report = _report([_open_followup("f-2")])

    dangling = _dangling(report)
    assert dangling, report["wiring_findings"]
    assert dangling[0]["extra"]["ref"] == "y@f-1"
    assert dangling[0]["extra"]["section"] == "s1"
    # A wait nothing can satisfy still holds the section rather than reading as
    # no edge at all.
    assert _verdict(report, "x")["ready"] is False


def test_a_followup_wait_on_a_live_followup_raises_no_finding():
    report = _report([_open_followup()])

    assert _dangling(report) == []
