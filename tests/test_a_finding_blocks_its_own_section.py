"""An unanswered plan-review finding blocks only the sections it is about."""

import json
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import plan_review, recovery, review, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414

SECTIONS = ("s2", "s4", "s5", "s5-1", "s7", "s19", "s21", "delivery")

PROSE = {
    "s2": "Read the plan once.",
    "s4": "Cache the parse.",
    "s5": "Charge each named path.",
    "s5-1": "Charge a directory once.",
    "s7": "Measure the cost.",
    "s19": "Admit the urgent fix.",
    "s21": "Report the count.",
    "delivery": "Extend the existing mechanism.",
}

PLAN = "docs/plans/fixture.html"
CONTENT_VERSION = 4


def _report(items) -> str:
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


@pytest.fixture
def many_section_plan(project):
    """A plan of seven implementable sections sharing one introduction."""
    home, repo, path = project
    body = "".join(
        f'<h2 id="{name}">Section {name}</h2><p>{PROSE[name]}</p>' for name in SECTIONS
    )
    text = path.read_text().replace(
        '<h2 id="delivery">Delivery</h2><p>Extend the existing mechanism.</p>',
        f"<p>Shared introduction.</p>{body}",
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = dict.fromkeys(SECTIONS, "implementable")
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
        for name in SECTIONS
    ]
    state["decisions"] = {
        "choice": {
            "question": "Which owner?",
            "choice": "shared",
            "rationale": "Reuse the owner.",
        }
    }
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _line_of(path, marker: str) -> int:
    """The 1-based line the marker's sentence is on, in the plan as it stands."""
    for number, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if marker in line:
            return number
    raise AssertionError(f"{marker!r} is not in the plan")


def _subject(path, run_id: str, rubric: str) -> dict:
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


def _store(
    path, *, rubric: str, run_id: str, findings, version: int | None = None
) -> dict:
    """Store one complete review of the plan as it stands now.

    ``version`` names the plan version the record is keyed on. The gate reads
    the newest covering record as the content review, so a content review
    stored beside a design review of the same version is keyed one higher —
    the design review is the plan's one search and never supersedes it.
    """
    items = (
        review.PLAN_DESIGN_REVIEW_ITEMS
        if rubric == "design"
        else review.PLAN_REVIEW_ITEMS
    )
    fields = recovery._review_dispatch_fields(
        _subject(path, run_id, rubric), rubric=rubric
    )
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    if version is not None:
        sidecar["plan_version"] = version
    Path(sidecar["report_path"]).write_text(_report(items))
    record = {
        **sidecar,
        "findings": list(findings),
        "responses": {},
        "review_run_id": run_id,
    }
    plan_review.store_plan_review(record)
    return record


def _gate(repo, section: str):
    return routing.require_plan_reviewed(
        node=TaskNode(
            id=f"build-{section}",
            goal=f"Build {section}",
            plan="fixture",
            section=section,
            role="implement",
        ),
        project="sample",
        repo=repo,
        authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
        enforce=True,
    )


def _empty_design(path) -> dict:
    """A design review that raised nothing, so only the content review holds."""
    return _store(path, rubric="design", run_id="review-design", findings=[])


def _two_section_findings(path):
    """Findings anchored on plan lines inside §4 and §21, left unanswered."""
    _empty_design(path)
    return _store(
        path,
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[
            {
                "id": "reasoning-1",
                "type": "reasoning",
                "anchor": f"{PLAN}:{_line_of(path, PROSE['s4'])}",
                "text": "§4's premise names a counter the code cannot observe.",
            },
            {
                "id": "done_when-1",
                "type": "done_when",
                "anchor": f"{PLAN}:{_line_of(path, PROSE['s21'])}",
                "text": "§21's done-when declares no negative control.",
            },
        ],
    )


def test_a_dispatch_of_an_untouched_section_passes(many_section_plan):
    _two_section_findings(many_section_plan[2])
    assert _gate(many_section_plan[1], "s19") is None


def test_a_dispatch_of_the_section_a_finding_is_about_is_refused(many_section_plan):
    _two_section_findings(many_section_plan[2])
    with pytest.raises(PlanReviewMissingError) as raised:
        _gate(many_section_plan[1], "s4")
    assert "reasoning-1" in str(raised.value)
    assert "done_when-1" not in str(raised.value)


def test_a_finding_that_names_no_section_refuses_every_section(many_section_plan):
    _empty_design(many_section_plan[2])
    _store(
        many_section_plan[2],
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[
            {
                "id": "document-1",
                "type": "reasoning",
                "anchor": "reckon/crew/routing.py",
                "text": "the plan's premise about the admission gate is false.",
            }
        ],
    )
    for section in ("s4", "s19"):
        with pytest.raises(PlanReviewMissingError, match="document-1"):
            _gate(many_section_plan[1], section)


def _sourced_design_findings(path):
    """A design review's findings, anchored in source files, naming sections.

    The anchors are the real shape a design review writes: a source file and a
    line, while the section the finding is about is named in its own text. The
    plan's own numbered sections are substituted for the ones the record was
    written against.
    """
    return [
        {
            "id": "reuse_search-1",
            "type": "reuse_search",
            "anchor": "reckon/roadmap.py:1079",
            "text": "§2 says the dependency edges are new work, but the resolver "
            "already parses every entry.",
        },
        {
            "id": "duplicate_owner-3",
            "type": "duplicate_owner",
            "anchor": "reckon/crew/routing.py:3122",
            "text": "§5's done-when is already the contract of the declared-input "
            "reader, so the section must cite that owner.",
        },
        {
            "id": "duplicate_owner-4",
            "type": "duplicate_owner",
            "anchor": "reckon/crew/routing.py:3165",
            "text": "§7's measure re-derives the count the same reader keeps.",
        },
        {
            "id": "interface_budget-1",
            "type": "interface_budget",
            "anchor": f"{PLAN}#s2",
            "text": "§2 and §7 both add a public surface.",
        },
    ]


def test_a_sourced_finding_blocks_each_section_it_names(many_section_plan):
    _store(
        many_section_plan[2],
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[],
    )
    record = _store(
        many_section_plan[2],
        rubric="design",
        run_id="review-design",
        findings=_sourced_design_findings(many_section_plan[2]),
    )
    assert plan_review.finding_units(record, "duplicate_owner-3") == ["s5"]
    assert plan_review.finding_units(record, "duplicate_owner-4") == ["s7"]
    assert plan_review.finding_units(record, "interface_budget-1") == ["s2"]

    with pytest.raises(PlanReviewMissingError) as raised:
        _gate(many_section_plan[1], "s5")
    assert "duplicate_owner-3" in str(raised.value)
    assert "duplicate_owner-4" not in str(raised.value)


def test_a_sourced_finding_with_no_section_named_refuses_every_section(
    many_section_plan,
):
    findings = _sourced_design_findings(many_section_plan[2])
    findings[1] = {
        **findings[1],
        "text": "the done-when is already the contract of the declared-input reader.",
    }
    _store(
        many_section_plan[2],
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[],
    )
    record = _store(
        many_section_plan[2],
        rubric="design",
        run_id="review-design",
        findings=findings,
    )
    assert plan_review.finding_units(record, "duplicate_owner-3") == [
        plan_review.DOCUMENT_UNIT
    ]
    for section in ("s5", "s19"):
        with pytest.raises(PlanReviewMissingError, match="duplicate_owner-3"):
            _gate(many_section_plan[1], section)


def test_a_source_line_does_not_resolve_to_the_plan_section_it_falls_in(
    many_section_plan,
):
    inside_s4 = _line_of(many_section_plan[2], PROSE["s4"])
    record = _store(
        many_section_plan[2],
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[
            {
                "id": "duplicate_owner-3",
                "type": "duplicate_owner",
                "anchor": f"reckon/crew/routing.py:{inside_s4}",
                "text": "a second reader of the same file already owns this.",
            }
        ],
    )
    assert plan_review.finding_units(record, "duplicate_owner-3") == [
        plan_review.DOCUMENT_UNIT
    ]
    assert "s4" not in plan_review.unanswered_findings_by_unit(record)


def test_a_dotted_subsection_name_blocks_its_own_record(many_section_plan):
    """§5.1 addresses the section whose record id is s5-1, and not s5."""
    _empty_design(many_section_plan[2])
    record = _store(
        many_section_plan[2],
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[
            {
                "id": "duplicate_owner-3",
                "type": "duplicate_owner",
                "anchor": "reckon/crew/routing.py:3122",
                "text": "§5.1 re-derives the directory charge its owner already makes.",
            }
        ],
    )
    assert plan_review.finding_units(record, "duplicate_owner-3") == ["s5-1"]
    with pytest.raises(PlanReviewMissingError, match="duplicate_owner-3"):
        _gate(many_section_plan[1], "s5-1")
    assert _gate(many_section_plan[1], "s5") is None


def test_a_plan_line_anchor_outranks_the_sections_its_text_names(many_section_plan):
    """A plan-file line settles the unit; text names only the ones it can settle.

    Both findings below sit on a §4 line. The first names no section, so only
    the line can place it; the second names §5, which the line contradicts.
    Resolving the text first would send the first to the document unit and the
    second to §5, so this case fails if the line is not read before the text.
    """
    _empty_design(many_section_plan[2])
    record = _store(
        many_section_plan[2],
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[
            {
                "id": "reasoning-1",
                "type": "reasoning",
                "anchor": f"{PLAN}:{_line_of(many_section_plan[2], PROSE['s4'])}",
                "text": "the premise names a counter the code cannot observe.",
            },
            {
                "id": "duplicate_owner-3",
                "type": "duplicate_owner",
                "anchor": f"{PLAN}:{_line_of(many_section_plan[2], PROSE['s4'])}",
                "text": "§5 re-derives the charge its owner already makes.",
            },
        ],
    )
    assert plan_review.finding_units(record, "reasoning-1") == ["s4"]
    assert plan_review.finding_units(record, "duplicate_owner-3") == ["s4"]
    with pytest.raises(PlanReviewMissingError, match="reasoning-1"):
        _gate(many_section_plan[1], "s4")
    with pytest.raises(PlanReviewMissingError, match="duplicate_owner-3"):
        _gate(many_section_plan[1], "s4")
    assert _gate(many_section_plan[1], "s5") is None


def test_a_mixed_case_anchor_fragment_names_its_section(many_section_plan):
    """#Delivery addresses the section whose record id is delivery."""
    _empty_design(many_section_plan[2])
    record = _store(
        many_section_plan[2],
        rubric="content",
        run_id="review-content",
        version=CONTENT_VERSION,
        findings=[
            {
                "id": "reasoning-1",
                "type": "reasoning",
                "anchor": f"{PLAN}#Delivery",
                "text": "the delivery step names no owner.",
            }
        ],
    )
    assert plan_review.finding_units(record, "reasoning-1") == ["delivery"]
    with pytest.raises(PlanReviewMissingError, match="reasoning-1"):
        _gate(many_section_plan[1], "delivery")
    assert _gate(many_section_plan[1], "s4") is None
