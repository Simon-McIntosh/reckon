"""The fingerprint accepts its surviving forms, and the collapse op is gone.

A review stays valid while the plan's fingerprint still matches it. The
fingerprint used to accept a third, pre-collapse digest — the older definition
that kept each section's lifecycle declaration and its landed card — because a
landing beat used to collapse a section's body into a card. The beat now ticks
a section instead, so the pre-collapse form and the ``collapse_section`` op it
existed for are retired. These cases pin the surviving forms and the refusal.
"""

import json
from pathlib import Path

from reckon import _plan_html, _store, mcp
from reckon.crew import plan_review, recovery
from tests.test_review_plan_command import project as project  # noqa: PLC0414


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


def _store_review(path, fingerprint, *, run_id):
    """Store a digest-only review under one fingerprint, with no snapshot."""
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


def test_only_the_section_set_and_whole_document_forms_remain(project):
    path = _landed(project)
    forms = plan_review._fingerprint_forms(path)
    assert len(forms) == 2
    assert forms[0] == plan_review.plan_fingerprint(path)
    # The retired pre-collapse definition is no longer among the accepted
    # digests, so a review stored under it no longer matches the plan.
    assert _retired_pre_collapse_digest(path) not in forms


def test_a_plan_whose_collapsed_card_is_unchanged_keeps_its_review(project):
    path = _landed(project)
    whole = plan_review._fingerprint_forms(path)[1]
    _store_review(path, whole, run_id="review-whole-document")
    records, uncovered, _ = plan_review.review_coverage("sample", "fixture", plan=path)
    assert [record["plan_fingerprint"] for record in records] == [whole]
    assert uncovered == set()


def test_a_review_under_the_retired_form_no_longer_matches(project):
    path = _landed(project)
    retired = _retired_pre_collapse_digest(path)
    _store_review(path, retired, run_id="review-pre-collapse")
    records, uncovered, _ = plan_review.review_coverage("sample", "fixture", plan=path)
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
