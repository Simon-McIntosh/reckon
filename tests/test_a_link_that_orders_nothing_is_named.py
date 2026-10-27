"""A link that orders nothing is named, and the connections it implies are too.

Two informational wiring findings exist so a review can judge removal
candidates rather than hunt for them. ``settled-link`` names a declaration whose
target has landed and which nothing still waits through. ``redundant-link``
names a declared edge a path of other declared edges already implies.

The control is the settled case's other half: the same two edges with an open
section still waiting on the target raise nothing, because that section's work
is still read against the link. Without that guard every satisfied edge in a
live plan would be offered for removal, which is the defect this pair of tests
measures from both directions.
"""

from __future__ import annotations

from reckon import roadmap

PROJECT = "sample"


def _plan(
    slug: str,
    *,
    status: str = "active",
    depends_on: list[str] | None = None,
    after: list[str] | None = None,
    sections: dict[str, str] | None = None,
    section_depends_on: dict[str, list[str]] | None = None,
) -> dict:
    row = {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": status,
        "impl": 0.0,
        "depends_on": depends_on or [],
        "effort": "M",
        "roi": "high",
        "blocking": [],
        "gates": [],
        "followups": [],
        "section_declarations": sections if sections is not None else {},
        "sections": [],
    }
    if after is not None:
        row["after"] = after
    if section_depends_on is not None:
        row["section_depends_on"] = section_depends_on
    return row


def _findings(report: dict, code: str, slug: str | None = None) -> list[dict]:
    return [
        finding
        for finding in report["wiring_findings"]
        if finding.get("code") == code
        and (slug is None or finding.get("slug") == slug)
    ]


def _roadmap(rows: list[dict]) -> dict:
    return roadmap.build_roadmap(PROJECT, rows, [])


def test_a_satisfied_depends_on_and_after_link_to_a_closed_plan_are_settled():
    report = _roadmap(
        [
            _plan(
                "waiting",
                depends_on=["landed"],
                after=["landed"],
                sections={"s1": "done"},
            ),
            _plan("landed", status="shipped", sections={"s1": "done"}),
        ]
    )

    findings = _findings(report, "settled-link")
    assert len(findings) == 2

    by_field = {finding["extra"]["field"]: finding for finding in findings}
    assert set(by_field) == {"depends_on", "after"}
    for field, finding in by_field.items():
        assert finding["severity"] == "info"
        assert finding["slug"] == "waiting"
        assert finding["extra"]["ref"] == "landed"
        assert finding["extra"]["target"] == "landed"
        assert finding["extra"]["target_status"] == "shipped"
        assert finding["extra"]["set"] == f"set {field} []"
        assert "landed" in finding["message"]
        assert finding["extra"]["set"] in finding["message"]


def test_the_set_a_finding_names_keeps_the_links_it_does_not_remove():
    report = _roadmap(
        [
            _plan(
                "waiting",
                depends_on=["landed", "elsewhere"],
                sections={"s1": "done"},
            ),
            _plan("landed", status="shipped", sections={"s1": "done"}),
            _plan("elsewhere"),
        ]
    )

    findings = _findings(report, "settled-link")
    assert [finding["extra"]["ref"] for finding in findings] == ["landed"]
    assert findings[0]["extra"]["set"] == 'set depends_on ["elsewhere"]'


def test_the_same_edges_raise_nothing_while_an_open_section_still_waits():
    report = _roadmap(
        [
            _plan(
                "waiting",
                depends_on=["landed"],
                after=["landed"],
                sections={"s1": "implementable"},
                section_depends_on={"s1": ["landed#s1"]},
            ),
            _plan("landed", status="shipped", sections={"s1": "done"}),
        ]
    )

    assert _findings(report, "settled-link") == []


def test_a_closed_section_wait_on_a_landed_section_is_settled():
    report = _roadmap(
        [
            _plan(
                "waiting",
                sections={"s1": "done"},
                section_depends_on={"s1": ["landed#s1"]},
            ),
            _plan("landed", status="shipped", sections={"s1": "done"}),
        ]
    )

    findings = _findings(report, "settled-link")
    assert len(findings) == 1
    finding = findings[0]
    assert finding["extra"]["section"] == "s1"
    assert finding["extra"]["ref"] == "landed#s1"
    assert finding["extra"]["set"] == "set section_depends_on.s1 []"


def test_a_declared_edge_a_path_already_implies_is_reported_once():
    report = _roadmap(
        [
            _plan("a", depends_on=["b", "c"]),
            _plan("b", depends_on=["c"]),
            _plan("c"),
        ]
    )

    findings = _findings(report, "redundant-link")
    assert len(findings) == 1
    finding = findings[0]
    assert finding["severity"] == "info"
    assert finding["slug"] == "a"
    assert finding["extra"]["ref"] == "c"
    assert finding["extra"]["target"] == "c"
    assert finding["extra"]["set"] == 'set depends_on ["b"]'
    assert "implied" in finding["message"]


def test_a_chain_without_the_direct_edge_reports_no_redundancy():
    report = _roadmap(
        [
            _plan("a", depends_on=["b"]),
            _plan("b", depends_on=["c"]),
            _plan("c"),
        ]
    )

    assert _findings(report, "redundant-link") == []


def test_an_edge_to_a_landed_target_is_settled_rather_than_redundant():
    report = _roadmap(
        [
            _plan("a", depends_on=["b", "landed"], sections={"s1": "done"}),
            _plan("b", depends_on=["landed"]),
            _plan("landed", status="shipped", sections={"s1": "done"}),
        ]
    )

    assert _findings(report, "redundant-link") == []