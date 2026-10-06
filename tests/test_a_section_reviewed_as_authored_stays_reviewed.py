"""Review coverage follows authored units while completed sections leave the gate."""

import json
from pathlib import Path

import pytest

from reckon import _plan_html, mcp, roadmap
from reckon.crew import plan_review, recovery, routing
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414


@pytest.fixture
def section_plan(project):
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
    state["decisions"] = {
        "choice": {
            "question": "Which owner?",
            "choice": "shared",
            "rationale": "Reuse the owner.",
        }
    }
    path.write_text(_plan_html.write_state(text, state))
    return home, repo, path


def _subject(path, *, run_id="review-authored-content"):
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


def _review(path, *, store=True, version=None, findings=()):
    # Each review writes its own snapshot under its own run id, so a plan
    # reviewed twice in one case carries two snapshots rather than one
    # overwriting the other.
    run_id = (
        "review-authored-content"
        if version is None
        else f"review-authored-content-v{version}"
    )
    fields = recovery._review_dispatch_fields(_subject(path, run_id=run_id))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    if version is not None:
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


def _gate(repo):
    return routing.require_plan_reviewed(
        node=TaskNode(
            id="build-beta",
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


def _collapse(repo, path):
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        checkout_path=str(repo),
        doc_type="plan",
        mode="state",
        expected_version=_plan_html.read_state(path.read_text())["version"],
        ops=[
            {
                "op": "collapse_section",
                "section": "a",
                "summary": "Alpha built.",
                "evidence_anchor": "/sample/evidence/archive/fixture#alpha",
            }
        ],
    )
    assert result.get("ok"), result
    assert 'class="section-landed"' in path.read_text()


def test_collapse_keeps_coverage(section_plan):
    _, repo, path = section_plan
    _review(path)
    assert _gate(repo) is None
    _collapse(repo, path)
    assert _gate(repo) is None


def _coverage(path):
    return plan_review._review_coverage("sample", "fixture", plan=path)


def test_authored_edit_uncovers_only_its_section(section_plan):
    _, repo, path = section_plan
    _review(path)
    path.write_text(path.read_text().replace("Build beta.", "Extend beta."))
    records, uncovered = _coverage(path)
    assert records and uncovered == {"b"}
    with pytest.raises(PlanReviewMissingError, match="uncovered units: b;"):
        _gate(repo)


def test_decision_edit_uncovers_only_the_document(section_plan):
    _, repo, path = section_plan
    _review(path)
    text = path.read_text()
    state = _plan_html.read_state(text)
    state["decisions"]["choice"]["rationale"] = "Choose a different owner."
    path.write_text(_plan_html.write_state(text, state))
    records, uncovered = _coverage(path)
    assert records and uncovered == {"_document"}
    with pytest.raises(PlanReviewMissingError, match="uncovered units: _document;"):
        _gate(repo)


@pytest.mark.parametrize("form", [1, 2], ids=["pre-collapse", "whole-document"])
def test_digestless_review_covers_only_an_unchanged_plan(section_plan, form):
    _, repo, path = section_plan
    record = _review(path)
    record.pop("section_digests")
    record.update(
        plan_fingerprint=plan_review._fingerprint_forms(path)[form],
        findings=[],
        responses={},
        review_run_id=_subject(path)["run_id"],
    )
    plan_review.store_plan_review(record)
    # A fingerprint-only record: its snapshot is removed too, so the coverage
    # predicate falls back to the stored digests the record does not carry
    # rather than recomputing them from the snapshot. What remains is the
    # whole-document fingerprint, which certifies only an unchanged plan.
    plan_review.review_report_directory(
        "sample", "fixture", _subject(path)["run_id"]
    ).joinpath(plan_review._REVIEW_SNAPSHOT_NAME).unlink()
    assert _coverage(path)[1] == set()
    assert _gate(repo) is None
    path.write_text(path.read_text().replace("Build beta.", "Extend beta."))
    records, uncovered = _coverage(path)
    assert records == []
    assert uncovered == {"a", "b", "_document"}
    with pytest.raises(
        PlanReviewMissingError, match="uncovered units: _document, a, b;"
    ):
        _gate(repo)


@pytest.mark.parametrize("collapse", [False, True])
def test_unstored_sidecar_is_promoted_before_coverage(section_plan, collapse):
    _, repo, path = section_plan
    sidecar = _review(path, store=False)
    assert plan_review.list_plan_reviews("sample") == []
    if collapse:
        _collapse(repo, path)
    assert _gate(repo) is None
    records = plan_review.list_plan_reviews("sample")
    assert len(records) == 1
    assert records[0]["section_digests"] == sidecar["section_digests"]


@pytest.mark.parametrize("collapse", [False, True], ids=["whole-match", "joined-units"])
def test_newest_covering_review_owns_findings(section_plan, collapse):
    _, repo, path = section_plan
    _review(path, version=2, findings=[{"id": "unanswered", "type": "wiring"}])
    if collapse:
        path.write_text(path.read_text().replace("Build alpha.", "Extend alpha."))
    _review(path, version=4)
    if collapse:
        _collapse(repo, path)
    records, uncovered = _coverage(path)
    assert uncovered == set()
    assert max(record["plan_version"] for record in records) == 4
    assert _gate(repo) is None


@pytest.mark.parametrize("dotted", [False, True])
def test_outstanding_units_follow_roadmap_declarations(section_plan, dotted):
    _, repo, path = section_plan
    _collapse(repo, path)
    text = path.read_text()
    if dotted:
        text = text.replace('"b"', '"s5.1"')
    state = _plan_html.read_state(text)
    if dotted:
        state["section_declarations"] = {"a": "done", "s5.1": "implementable"}
    state["section_declarations"]["finished"] = "done"
    path.write_text(_plan_html.write_state(text, state))
    declarations = _plan_html.read_state(path.read_text())["section_declarations"]
    expected = {
        _plan_html.section_record_id(key)
        for key in roadmap.implementable_sections(declarations)
    }
    assert expected == ({"s5-1"} if dotted else {"b"})
    assert _coverage(path)[1] == expected | {"_document"}
    _review(path)
    assert _coverage(path)[1] == set()


@pytest.mark.parametrize(
    "markup, expected",
    [
        ("a<b>b</b>&amp; c", "a b & c"),
        (
            "text<script>hidden <b>script</b></script><style>hidden</style> tail",
            "text tail",
        ),
        (
            "<head><title>Hidden</title></head><p>Visible&nbsp; text.</p>",
            "Visible text.",
        ),
        ("before<!-- hidden -->after", "before after"),
    ],
)
def test_strip_tags_extracts_only_prose(markup, expected):
    assert _plan_html.strip_tags(markup) == expected


def test_entity_only_rationale_keeps_inventory_decision_closed():
    markup = '<div class="r-dec" data-choice=""><p class="r-dec-rat">&nbsp;</p></div>'
    assert _plan_html.strip_tags("&nbsp;") == ""
    assert _plan_html.count_open_decisions(markup) == 0
    assert _plan_html.count_open_decisions(markup.replace("&nbsp;", "")) == 1


def test_marked_spans_are_opt_in_and_keep_nested_elements_whole():
    markup = '<body><div data-reckon="landed"><div>Nested</div>Tail</div><p>Authored</p></body>'
    assert _plan_html.structured_section_spans(markup) == ()
    spans = _plan_html.structured_section_spans(markup, every_marked_element=True)
    assert [markup[a:b] for a, b in spans] == [
        '<div data-reckon="landed"><div>Nested</div>Tail</div>'
    ]
    assert (
        " ".join(
            _plan_html.strip_tags(raw) for _, raw in plan_review._prose_slices(markup)
        ).strip()
        == "Authored"
    )


def test_headingless_and_marked_sections_keep_no_prose():
    markup = (
        '<body><section class="section-landed"><p>Landed.</p></section>'
        '<section id="a" data-reckon="section"><h2>Alpha</h2><p>Record.</p></section>'
        '<h2 id="b">Beta</h2><p>Authored.</p></body>'
    )
    slices = list(plan_review._prose_slices(markup))
    assert (
        " ".join(text for _, raw in slices if (text := _plan_html.strip_tags(raw)))
        == "Beta Authored."
    )
    assert "a" in plan_review._section_digests(markup)


def test_complementary_reviews_cover_the_union_of_authored_units(section_plan):
    _, repo, path = section_plan
    original = path.read_text()
    path.write_text(original.replace("Build alpha.", "Extend alpha."))
    _review(path, version=3)
    path.write_text(original.replace("Build beta.", "Extend beta."))
    _review(path, version=4)
    path.write_text(
        original.replace("Build alpha.", "Extend alpha.").replace(
            "Build beta.", "Extend beta."
        )
    )
    records, uncovered = _coverage(path)
    assert uncovered == set()
    assert {record["plan_version"] for record in records} == {3, 4}
    assert _gate(repo) is None


def test_dotted_section_digest_joins_after_another_section_completes(section_plan):
    _, repo, path = section_plan
    text = path.read_text().replace('id="b"', 'id="s5.1"')
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"a": "implementable", "s5.1": "implementable"}
    state["sections"] = [
        {**record, "id": "s5.1"} if record["id"] == "b" else record
        for record in state["sections"]
    ]
    path.write_text(_plan_html.write_state(text, state))
    record = _review(path)
    assert "s5-1" in record["section_digests"]
    text = path.read_text().replace("Build alpha.", "Alpha completed.")
    state = _plan_html.read_state(text)
    state["section_declarations"]["a"] = "done"
    for section in state["sections"]:
        if section["id"] == "a":
            section["status"] = "done"
    path.write_text(_plan_html.write_state(text, state))
    assert _coverage(path)[1] == set()
    assert _gate(repo) is None
