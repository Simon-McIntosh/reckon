"""A plan review is earned by significant change, measured per unit.

A stored review covers each authored unit — a section, or the document unit
carrying its decisions — when that unit's prose still matches the review's own
snapshot, exactly or within the declared change fraction of its words. New
sections, changed done-whens and sections that became implementable since the
review are material whatever their size, and a review with no snapshot covers
only an exact digest match. These cases drive the predicate, the gate refusal
and the flight layer that declares the threshold; the fixture synthesises a
mounted project under ``RECKON_HOME``, so nothing here reads the operator's
crew home.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from reckon import _plan_html, flight
from reckon.crew import plan_review, recovery, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414

THRESHOLD = 0.30
SNAPSHOT_RUN_ID = "review-earned-by-change"

# Captured before any fixture runs: the shared ``project`` fixture patches
# ``flight.resolve`` to a stub, and a case that must exercise the real layer
# resolution restores this reference for its own scope.
_REAL_RESOLVE = flight.resolve


def _plain(prefix: str, count: int = 25) -> str:
    """Distinct, single-token words, so a substitution moves an exact share."""
    return " ".join(f"{prefix}{index:02d}" for index in range(count))


PLAIN_A = _plain("alpha")
PLAIN_B = _plain("bravo")
PLAIN_C = _plain("charlie")
DECISION_RATIONALE = "Reuse the existing owner rather than invent a second one"
DONE_WHEN = "charlie ships and nothing regresses"


def _base_html() -> str:
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        "</head><body>"
        "<p>Shared introduction text.</p>"
        f'<h2 id="a">Alpha</h2><p>{PLAIN_A}</p>'
        f'<h2 id="b">Beta</h2><p>{PLAIN_B}</p>'
        f'<h2 id="c">Charlie</h2><p>{PLAIN_C}</p>'
        f"<p><strong>Done when</strong>: {DONE_WHEN}.</p>"
        "</body></html>"
    )


def _section_records(names):
    return [
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
        for name in names
    ]


@pytest.fixture
def sectioned(project):
    home, repo, path = project
    path.write_text(_base_html(), encoding="utf-8")
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_declarations"] = {
        "a": "implementable",
        "b": "implementable",
        "c": "implementable",
    }
    state["sections"] = _section_records(("a", "b", "c"))
    state["decisions"] = {
        "choice": {
            "question": "Which owner?",
            "choice": "shared",
            "rationale": DECISION_RATIONALE,
        }
    }
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path, *, run_id=SNAPSHOT_RUN_ID):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": "content",
        "local": True,
        "run_id": run_id,
    }


def _review(path, *, store=True, run_id=SNAPSHOT_RUN_ID, version=3, findings=()):
    fields = recovery._review_dispatch_fields(_subject(path, run_id=run_id))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    sidecar["plan_version"] = version
    Path(sidecar["report_path"]).write_text("RUBRIC wiring: pass\n")
    if store:
        plan_review.store_plan_review(
            {
                **sidecar,
                "findings": list(findings),
                "responses": {},
                "review_run_id": run_id,
            }
        )
    return sidecar


def _coverage(path, *, config=None):
    return plan_review._review_coverage("sample", "fixture", plan=path, config=config)


def _gate(repo):
    return routing.require_plan_reviewed(
        node=TaskNode(
            id="build-b",
            goal="Build beta",
            plan="fixture",
            section="b",
            role="implement",
        ),
        project="sample",
        repo=repo,
        authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
        enforce=True,
    )


def _section_paragraph_match(path, section):
    """Return the section's document text and its paragraph match.

    The state writer injects a ``<section>`` marker between a heading and its
    paragraph, so the paragraph is located as the first one after the heading
    rather than as its immediate sibling. The measure reads the heading text
    too, but a heading is a label, so the edits here move only the paragraph.
    """
    text = path.read_text()
    heading = re.search(rf'<h2 id="{re.escape(section)}">[^<]*</h2>', text)
    assert heading, f"section {section} heading not found"
    paragraph = re.compile(r"<p>([^<]*)</p>").search(text, heading.end())
    assert paragraph, f"section {section} paragraph not found"
    return text, paragraph


def _section_paragraph(path, section):
    return _section_paragraph_match(path, section)[1].group(1).split()


def _rewrite_section(path, section, words):
    text, paragraph = _section_paragraph_match(path, section)
    path.write_text(
        text[: paragraph.start(1)] + " ".join(words) + text[paragraph.end(1) :]
    )


def test_small_edit_stays_covered_and_large_edit_uncovers(sectioned):
    _, repo, path = sectioned
    _review(path)
    original = path.read_text()

    words = _section_paragraph(path, "b")
    total = len(plan_review._prose_texts(path)["b"].split())
    for index in (0, 1, 2):  # about a tenth of the section's words
        words[index] = f"small{index:02d}"
    _rewrite_section(path, "b", words)
    records, uncovered, _ = _coverage(path)
    assert uncovered == set(), "a small section edit stays covered"
    assert records and _gate(repo) is None

    path.write_text(original)
    words = _section_paragraph(path, "b")
    for index in range(9):  # about a third: past the threshold
        words[index] = f"large{index:02d}"
    _rewrite_section(path, "b", words)
    records, uncovered, changes = _coverage(path)
    assert uncovered == {"b"}
    assert changes["b"] == pytest.approx(9 / total)
    with pytest.raises(
        PlanReviewMissingError,
        match=r"uncovered units: b;.*b changed by 35% of its words",
    ):
        _gate(repo)


def test_three_successive_edits_accumulate_against_the_reviewed_snapshot(sectioned):
    _, _repo, path = sectioned
    _review(path)
    words = _section_paragraph(path, "b")
    seen = []
    for count in (3, 6, 9):
        for index in range(count - 3, count):
            words[index] = f"step{index:02d}"
        _rewrite_section(path, "b", words)
        seen.append(_coverage(path)[1])
    assert seen[0] == set()
    assert seen[1] == set()
    assert seen[2] == {"b"}, "the third step crosses the threshold cumulatively"


def test_an_added_section_is_never_covered(sectioned):
    _, _repo, path = sectioned
    _review(path)
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_declarations"]["d"] = "implementable"
    state["sections"] = _section_records(("a", "b", "c", "d"))
    text = text.replace(
        "<p><strong>Done when</strong>",
        '<h2 id="d">Delta</h2><p>delta00</p>\n<p><strong>Done when</strong>',
    )
    path.write_text(_plan_html.write_state(text, state))
    _, uncovered, _ = _coverage(path)
    assert "d" in uncovered, "a section absent from the review is never covered"


def test_a_decision_rewrite_uncovers_the_document_unit(sectioned):
    _, _repo, path = sectioned
    _review(path)
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["decisions"]["choice"]["rationale"] = (
        "The owner is now the second mechanism, and the choice that named it "
        "is replaced wholesale so that the document unit's digest moves."
    )
    path.write_text(_plan_html.write_state(text, state))
    _, uncovered, _ = _coverage(path)
    assert uncovered == {"_document"}, "a decision change is never covered by size"


def test_a_one_word_done_when_change_uncovers_its_section(sectioned):
    _, _repo, path = sectioned
    _review(path)
    path.write_text(path.read_text().replace("nothing regresses", "nothing breaks"))
    _, uncovered, _ = _coverage(path)
    assert uncovered == {"c"}, "a one-word done-when edit is material whatever its size"


def test_a_section_implementable_only_since_the_review_is_never_covered(sectioned):
    _, _repo, path = sectioned
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_declarations"]["b"] = "deferred"
    for record in state["sections"]:
        if record["id"] == "b":
            record["status"] = "deferred"
    path.write_text(_plan_html.write_state(text, state))
    _review(path)

    text = path.read_text()
    state = _plan_html.read_state(text)
    state["section_declarations"]["b"] = "implementable"
    for record in state["sections"]:
        if record["id"] == "b":
            record["status"] = "implementable"
    path.write_text(_plan_html.write_state(text, state))

    _, uncovered, _ = _coverage(path)
    assert uncovered == {"b"}, "a section that became implementable counts as new"


def test_a_review_without_a_snapshot_covers_only_an_exact_match(sectioned):
    _, repo, path = sectioned
    _review(path)
    plan_review.review_report_directory("sample", "fixture", SNAPSHOT_RUN_ID).joinpath(
        plan_review._REVIEW_SNAPSHOT_NAME
    ).unlink()
    # Unchanged content: the stored digest still matches exactly.
    assert _coverage(path)[1] == set()

    words = _section_paragraph(path, "b")
    words[0] = "tiny00"  # one word: a few percent, but there is no prose to measure
    _rewrite_section(path, "b", words)
    _, uncovered, _ = _coverage(path)
    assert uncovered == {"b"}
    with pytest.raises(PlanReviewMissingError, match=r"uncovered units: b;"):
        _gate(repo)


def test_a_project_override_changes_the_verdict(sectioned):
    _, _repo, path = sectioned
    _review(path)
    words = _section_paragraph(path, "b")
    for index in range(9):  # about a third: past the shipped threshold
        words[index] = f"big{index:02d}"
    _rewrite_section(path, "b", words)

    assert "b" in _coverage(path)[1]
    loose = {"review": {"plan_change_threshold": 0.5}}
    assert "b" not in _coverage(path, config=loose)[1]


def test_a_project_layer_threshold_changes_the_gate_verdict(sectioned, monkeypatch):
    _, repo, path = sectioned
    _review(path)
    words = _section_paragraph(path, "b")
    total = len(plan_review._prose_texts(path)["b"].split())
    for index in range(3):  # about a tenth of the section's words
        words[index] = f"small{index:02d}"
    _rewrite_section(path, "b", words)
    assert _gate(repo) is None, "a tenth stays covered under the shipped threshold"

    # Restore the real resolution and write the project's own layer where the
    # registered mount resolves it: the gate, which passes no config, must pick
    # the tighter threshold up through that path alone.
    monkeypatch.setattr(flight, "resolve", _REAL_RESOLVE)
    layer = flight.project_config_path("sample")
    layer.parent.mkdir(parents=True, exist_ok=True)
    layer.write_text("review:\n  plan_change_threshold: 0.05\n", encoding="utf-8")
    assert _REAL_RESOLVE(project="sample").config["review"][
        "plan_change_threshold"
    ] == (0.05)

    share_percent = round(100 * 3 / total)
    with pytest.raises(
        PlanReviewMissingError,
        match=rf"uncovered units: b;.*b changed by {share_percent}% of its words",
    ):
        _gate(repo)


def test_the_threshold_is_read_through_the_flight_accessor():
    assert flight.plan_review_change_threshold(None) == THRESHOLD
    assert flight.plan_review_change_threshold({}) == THRESHOLD
    assert (
        flight.plan_review_change_threshold({"review": {"plan_change_threshold": 0.4}})
        == 0.4
    )
    # A wrong shape is treated as the default rather than widening coverage.
    assert (
        flight.plan_review_change_threshold({"review": {"plan_change_threshold": 2}})
        == THRESHOLD
    )
    assert (
        flight.plan_review_change_threshold({"review": {"plan_change_threshold": "x"}})
        == THRESHOLD
    )


def test_validate_layer_refuses_shapes_outside_the_review_key_table():
    flight.validate_layer({"review": {"plan_change_threshold": 0.2}}, "test")
    with pytest.raises(flight.FlightConfigError, match="plan_change_threshold"):
        flight.validate_layer({"review": {"plan_change_threshold": 1.5}}, "test")
    with pytest.raises(flight.FlightConfigError, match="plan_change_threshold"):
        flight.validate_layer({"review": {"plan_change_threshold": -0.1}}, "test")


def test_the_schema_cleaner_and_validator_read_one_table():
    assert set(flight.REVIEW_OWNED_KEYS) == {"plan_change_threshold"}
    cleaned = flight._schema_view(
        {
            "review": {
                "plan_change_threshold": 0.3,
                "tiers": {},
            }
        }
    )
    assert cleaned["review"] == {"tiers": {}}


def test_rule_nine_names_the_coverage_predicate():
    text = (
        Path(__file__).parents[1] / "skills" / "reckon-edit" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "_review_coverage" in text
