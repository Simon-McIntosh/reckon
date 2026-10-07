"""A section ref resolves against every section the target declares.

A staged ref names a section the target plan authors. Three readers validate
one: ``section_dependency_refusals`` and the staged-ref checks the roadmap runs
for ``after`` edges, section-scoped ``depends_on`` edges and hard ``depends_on``
edges. Each must answer through the same authored set — the anchors gates and
comments bind, the ``section_declarations`` keys and the typed ``sections``
records — so a section carried only by a declaration or only by a record still
resolves. A ref to a section the target never declares still earns the finding,
or the wider set would be indistinguishable from no check at all.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from reckon._schema import section_dependency_refusals
from reckon.roadmap import _after_edges, _section_scoped_edges, build_roadmap

PROJECT = "section-ref-project"

# The two authored forms that once escaped the existence check: a classification
# declaration with no typed record, and a typed record with no declaration.
DECLARATION_ONLY = {"section_declarations": {"s8": "implementable"}}
RECORD_ONLY = {"sections": [{"id": "s8"}]}


def _consumer(**overrides) -> dict:
    row = {
        "slug": "consumer",
        "title": "Consumer",
        "type": "plan",
        "status": "active",
        "impl": 0.0,
        "depends_on": [],
        "after": [],
        "sprint": None,
        "effort": "M",
        "roi": "high",
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [],
    }
    row.update(overrides)
    return row


def _target(slug: str, **authored) -> dict:
    row = {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": "active",
        "impl": 0.0,
        "depends_on": [],
        "after": [],
        "sprint": None,
        "effort": "M",
        "roi": "high",
        "gates": [],
        "followups": [],
    }
    row.update(authored)
    return row


# --- section_dependency_refusals -------------------------------------------


def test_a_declaration_only_section_resolves() -> None:
    refusals = section_dependency_refusals(
        {"s1": ["target#s8"]},
        lambda project, slug: DECLARATION_ONLY,
        owning_project=PROJECT,
    )
    assert refusals == []


def test_a_record_only_section_resolves() -> None:
    refusals = section_dependency_refusals(
        {"s1": ["target#s8"]},
        lambda project, slug: RECORD_ONLY,
        owning_project=PROJECT,
    )
    assert refusals == []


def test_a_ref_to_an_undeclared_section_still_earns_the_finding() -> None:
    refusals = section_dependency_refusals(
        {"s1": ["target#s9"]},
        lambda project, slug: DECLARATION_ONLY,
        owning_project=PROJECT,
    )
    assert [row["code"] for row in refusals] == ["missing-dependency-section"]


# --- roadmap staged-ref sites (831 after, 947 section-scoped, 2470 hard) ----


def test_the_after_edge_resolves_a_declaration_and_a_record() -> None:
    plan = {"slug": "consumer", "after": ["target#s8"]}
    for authored in (DECLARATION_ONLY, RECORD_ONLY):
        rows = _after_edges(
            PROJECT, plan, {"target": _target("target", **authored)}, {}, None
        )
        assert rows and rows[0]["found"] is True
        assert rows[0]["section_found"] is True


def test_the_after_edge_still_reports_an_undeclared_section() -> None:
    plan = {"slug": "consumer", "after": ["target#s9"]}
    rows = _after_edges(
        PROJECT, plan, {"target": _target("target", **DECLARATION_ONLY)}, {}, None
    )
    assert rows[0]["section_found"] is False


def test_section_scoped_edges_resolve_a_declaration_and_a_record() -> None:
    for authored in (DECLARATION_ONLY, RECORD_ONLY):
        rows, findings = _section_scoped_edges(
            PROJECT,
            _consumer(),
            "consumer",
            {"s1": ["target#s8"]},
            {"target": _target("target", **authored)},
            None,
        )
        assert rows and rows[0]["section_found"] is True
        assert not [f for f in findings if f["code"] == "missing-dependency-section"]


def test_section_scoped_edges_still_find_an_undeclared_section() -> None:
    rows, findings = _section_scoped_edges(
        PROJECT,
        _consumer(),
        "consumer",
        {"s1": ["target#s9"]},
        {"target": _target("target", **DECLARATION_ONLY)},
        None,
    )
    assert rows[0]["section_found"] is False
    assert [f["code"] for f in findings] == ["missing-dependency-section"]


def _consumer_row(report: dict) -> dict:
    for group in ("ready_now", "blocked", "pending_work"):
        for row in report.get(group, []):
            if row["slug"] == "consumer":
                return row
    raise AssertionError("consumer row not found in roadmap report")


def test_the_hard_dependency_ref_resolves_a_declaration_and_a_record() -> None:
    for authored in (DECLARATION_ONLY, RECORD_ONLY):
        report = build_roadmap(
            PROJECT,
            [_consumer(depends_on=["target#s8"]), _target("target", **authored)],
            [],
        )
        row = _consumer_row(report)
        (edge,) = [e for e in row["depends_on"] if e.get("stage") == "s8"]
        assert edge["section_found"] is True
        assert not [
            f
            for f in report["wiring_findings"]
            if f["code"] == "missing-dependency-section"
        ]


def test_the_hard_dependency_ref_still_finds_an_undeclared_section() -> None:
    report = build_roadmap(
        PROJECT,
        [_consumer(depends_on=["target#s9"]), _target("target", **DECLARATION_ONLY)],
        [],
    )
    assert [
        f["code"]
        for f in report["wiring_findings"]
        if f["code"] == "missing-dependency-section"
    ] == ["missing-dependency-section"]


# --- the file-aware read: a row that carries neither field -------------------


def _write_target(docs: Path, slug: str, body: str, project: str = PROJECT) -> None:
    """Write one plan file the store's layout resolves, with the given body."""

    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = "".join(
        f'<meta name="{name}" content="{html.escape(value)}">'
        for name, value in (
            ("docs-project", project),
            ("reckon-type", "plan"),
            ("plan-slug", slug),
            ("plan-status", "active"),
            ("plan-modified", "2026-10-01"),
        )
    )
    path.write_text(
        "<!doctype html><html><head>"
        f"{head}<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{body}</main></body></html>',
        encoding="utf-8",
    )


def _bare_row(slug: str) -> dict:
    """A discovery-shaped row: no declarations, no typed records."""

    row = _target(slug)
    row.pop("sections", None)
    return row


def test_the_roadmap_reads_a_declaration_and_a_record_from_the_file(tmp_path) -> None:
    """A row carrying neither field resolves against the file it was read from."""

    declaration_docs = tmp_path / "declaration"
    _write_target(
        declaration_docs,
        "target",
        '<meta name="plan-section-declarations" content='
        f'"{html.escape(json.dumps({"s8": "implementable"}))}">',
    )
    record_docs = tmp_path / "record"
    _write_target(
        record_docs,
        "target",
        '<h2 id="s8">§8</h2>'
        '<section data-reckon="section" data-id="s8" data-status="implementable"'
        ' data-effort-hours="1" data-capability-version="1.0"'
        ' data-capability-class="general" data-capability-reasoning="standard"'
        ' data-capability-verification="strict" data-capability-risk="moderate">'
        "</section>",
    )

    for docs in (declaration_docs, record_docs):
        report = build_roadmap(
            PROJECT,
            [_consumer(depends_on=["target#s8"]), _bare_row("target")],
            [],
            docs_dir=docs,
        )
        row = _consumer_row(report)
        (edge,) = [e for e in row["depends_on"] if e.get("stage") == "s8"]
        assert edge["section_found"] is True, docs.name
        assert not [
            f
            for f in report["wiring_findings"]
            if f["code"] == "missing-dependency-section"
        ]
