"""The two forms a plan's fingerprint offers, and the refusal of the collapse op.

A stored plan review stays valid while the plan's fingerprint still matches it.
``_fingerprint_forms`` offers two digests of one snapshot: the section-set
digest, which names each authored unit, and the whole-document digest, which
reads the remaining document as one unit. A landing beat writes a card into a
section's body, so a review of the carded plan must keep covering it; the
``collapse_section`` op that once rewrote the body is refused as an unknown
verb. These cases pin the two forms, the coverage each carries, and the
refusal.
"""

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, mcp
from reckon.crew import plan_review, recovery
from tests.test_review_plan_command import project as project  # noqa: PLC0414


def _section_set_digest(path) -> str:
    """The section-set form, restated from the units it digests."""
    return plan_review._digest(plan_review._section_digests(path))


def _whole_document_digest(path) -> str:
    """The whole-document form, restated from state and the remaining prose.

    A landed card's interior is dropped, so the section's landed form counts
    neither as its own unit nor as document prose.
    """
    document = path.read_text(encoding="utf-8")
    return plan_review._digest(
        {
            "state": plan_review._digest_state(document),
            "prose": " ".join(
                prose
                for _, prose in _plan_html.section_prose(
                    document,
                    keep_landed_cards=bool(not _plan_html.landed_section_ids(document)),
                )
                if prose
            ),
        }
    )


def _landed(project):
    """The fixture plan with its one section collapsed to a genuine landed card."""
    _, _, path = project
    path.write_text(
        _store._collapse_authored_section(
            path.read_text(encoding="utf-8"),
            {
                "section": "delivery",
                "summary": "Delivery built.",
                "evidence_anchor": "/sample/evidence/archive/fixture#delivery",
            },
        ),
        encoding="utf-8",
    )
    assert _plan_html.landed_section_ids(path.read_text(encoding="utf-8")) == {
        "delivery"
    }
    return path


def _subject(path, *, run_id):
    return {
        "subject": "plan",
        "project": "sample",
        "plan_slug": "fixture",
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": "coordinator",
        "rubric": "design",
        "local": True,
        "run_id": run_id,
    }


def _review(path, *, run_id):
    """Store the review the dispatcher would compose for this plan.

    The sidecar the dispatch fields write carries the plan's own fingerprint and
    the snapshot the reviewer read, which is the shape production judges.
    """
    fields = recovery._review_dispatch_fields(_subject(path, run_id=run_id))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text("RUBRIC design: pass\n")
    plan_review.store_plan_review(
        {**sidecar, "findings": [], "responses": {}, "review_run_id": run_id}
    )
    return sidecar


def _store_fingerprint_only_review(path, fingerprint, *, run_id):
    """Store a record whose only evidence is one fingerprint: no snapshot, no units."""
    fields = recovery._review_dispatch_fields(_subject(path, run_id=run_id))
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    sidecar["plan_fingerprint"] = fingerprint
    sidecar.pop("section_digests", None)
    plan_review.review_report_directory("sample", "fixture", run_id).joinpath(
        plan_review._REVIEW_SNAPSHOT_NAME
    ).unlink(missing_ok=True)
    plan_review.store_plan_review(
        {**sidecar, "findings": [], "responses": {}, "review_run_id": run_id}
    )
    return sidecar


def _retired_pre_collapse_digest(path):
    """The retired form: declarations kept and landed-card interiors kept."""
    document = path.read_text(encoding="utf-8")
    payload = {
        "state": plan_review._digest_state(document, keep_declarations=True),
        "prose": " ".join(
            prose
            for _, prose in _plan_html.section_prose(document, keep_landed_cards=True)
            if prose
        ),
    }
    return plan_review._digest(payload)


def _coverage(path):
    records, uncovered, _changes = plan_review.review_coverage(
        "sample", "fixture", plan=path
    )
    return records, uncovered


def test_only_the_section_set_and_whole_document_forms_remain(project):
    path = _landed(project)
    forms = plan_review._fingerprint_forms(path)
    # The two definitions disagree, or one form would stand in for both.
    assert len(forms) == 2
    assert forms == (_section_set_digest(path), _whole_document_digest(path))
    assert forms[0] != forms[1]
    # The retired pre-collapse definition is no longer among the accepted
    # digests, so a review stored under it no longer matches the plan.
    assert _retired_pre_collapse_digest(path) not in forms


def test_a_review_of_a_carded_plan_keeps_covering_it(project):
    """The landing card is not authored design, so it owes no fresh review."""
    path = _landed(project)
    sidecar = _review(path, run_id="review-of-the-carded-plan")
    assert sidecar["plan_fingerprint"] == _section_set_digest(path)
    records, uncovered = _coverage(path)
    assert records, "a review of the carded plan must still cover it"
    assert uncovered == set()
    assert {record["plan_fingerprint"] for record in records} == {
        sidecar["plan_fingerprint"]
    }


@pytest.mark.parametrize(
    "form",
    [
        pytest.param(_section_set_digest, id="section-set"),
        pytest.param(_whole_document_digest, id="whole-document"),
    ],
)
def test_a_carded_plan_is_covered_under_each_surviving_form(project, form):
    """Each form is the whole-plan match a fingerprint-only review gets."""
    path = _landed(project)
    fingerprint = form(path)
    _store_fingerprint_only_review(
        path, fingerprint, run_id=f"review-{form.__name__.removeprefix('_')}"
    )
    records, uncovered = _coverage(path)
    assert [record["plan_fingerprint"] for record in records] == [fingerprint]
    assert uncovered == set()


def test_a_review_under_the_retired_form_no_longer_matches(project):
    path = _landed(project)
    _store_fingerprint_only_review(
        path, _retired_pre_collapse_digest(path), run_id="review-pre-collapse"
    )
    records, uncovered = _coverage(path)
    assert records == []
    assert uncovered


def test_collapse_section_is_refused_as_an_unknown_op(project):
    _, repo, path = project
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        expected_version=_plan_html.read_state(path.read_text(encoding="utf-8"))[
            "version"
        ],
        checkout_path=str(repo),
        doc_type="plan",
        mode="state",
        ops=[
            {
                "op": "collapse_section",
                "section": "delivery",
                "summary": "Built.",
                "evidence_anchor": "/sample/evidence/archive/fixture#delivery",
            }
        ],
    )
    assert result["ok"] is False
    assert result["error"] == "op_error"
    assert "unknown verb 'collapse_section'" in result["detail"]
