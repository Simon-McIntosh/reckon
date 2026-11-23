"""A plan holding a request on its review is never recorded as settled.

An admission pass holds a request on ``plan-review-owed`` when a plan unit the
request reads carries no stored review, and no lane releases it until that
review is composed. The review reflex weighs a plan once per edit and used to
pass over one it had already weighed at its current edit, so a plan weighed
without its review being composed kept its request waiting indefinitely.

The pass must therefore treat a held plan as unsettled: it composes the owed
review when a unit is uncovered, whatever edit the plan has already been
weighed at, and when it cannot it records why — a unit at its review cap, or
an unanswered finding with its id — for the session that owns the request. A
plan holding no such request keeps today's behaviour and is passed over at an
unchanged edit.
"""

from __future__ import annotations

import json

from reckon.crew import plan_review, runs
from tests.test_a_plan_review_composes_itself import (
    AUTHOR_SESSION,
    QUIET_EDIT_SECONDS,
    _edit,
    _launch,
    _pass,
    _run_naming,
)
from tests.test_a_queued_request_is_stamped_at_queue_time import (
    _reviewed,
    _store_content,
)
from tests.test_a_queued_request_is_stamped_at_queue_time import (
    two_section_plan as two_section_plan,  # noqa: PLC0414
)
from tests.test_review_plan_command import project as project  # noqa: PLC0414


def _hold_request(plan_slug: str, *, session: str = AUTHOR_SESSION) -> str:
    """Write the live pointer an admission pass leaves on a held request."""
    run_id = f"r-{plan_slug}-held"
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": "sample",
                "session": session,
                "phase": "held",
                "created_at": "2026-11-23T00:00:00+00:00",
                "node": {"id": f"build-{plan_slug}", "plan": plan_slug},
                "queue_hold": {
                    "prior_phase": "queued",
                    "started_at": "2026-11-23T00:00:00+00:00",
                    "reason": "plan-review-owed",
                    "detail": "the plan carries no stored review of its s2 unit",
                    "automatic": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return run_id


def _weigh_at_current_edit(path) -> float:
    """Record the plan as weighed at the edit it carries now, as a pass would."""
    edited = path.stat().st_mtime
    plan_review._record_weighed_edits("sample", {"fixture": edited})
    return edited


def _uncovered_plan(path) -> None:
    """Review the plan, then rewrite a section, so that unit is not covered."""
    _reviewed(path)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "<p>Build two.</p>",
            "<p>Build two, name the command it runs, and record what the "
            "command reported so a reader can check it.</p>",
        ),
        encoding="utf-8",
    )


def test_a_held_plan_with_an_uncovered_unit_is_composed_and_left_unsettled(
    two_section_plan, monkeypatch
):
    _home, _repo, path = two_section_plan
    launched = _launch(monkeypatch)
    _uncovered_plan(path)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming(session=AUTHOR_SESSION)
    _hold_request("fixture")
    _weigh_at_current_edit(path)

    report = _pass()

    assert [row["plan"] for row in report["composed"]] == ["fixture"], report
    assert report["composed"][0]["session"] == AUTHOR_SESSION
    assert launched == [AUTHOR_SESSION]
    # The plan is not recorded as settled: the request it holds is answered by
    # a stored review and admission's release, not by the pass's dispatch.
    assert "fixture" not in plan_review._weighed_edits("sample")


def test_a_second_pass_composes_no_second_review_for_the_same_version(
    two_section_plan, monkeypatch
):
    _home, _repo, path = two_section_plan
    _launch(monkeypatch)
    _uncovered_plan(path)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming(session=AUTHOR_SESSION)
    _hold_request("fixture")
    _weigh_at_current_edit(path)

    first = _pass()
    assert [row["plan"] for row in first["composed"]] == ["fixture"], first

    # The review the first pass composed delivers for the version it read.
    _store_content(path, "composed-review")
    second = _pass()

    assert [row["plan"] for row in second["composed"]] == [], second


def test_a_covered_plan_held_on_an_unanswered_finding_records_the_finding(
    two_section_plan, monkeypatch
):
    _home, _repo, path = two_section_plan
    _launch(monkeypatch)
    _reviewed(path)
    content = _store_content(path, "content-with-finding")
    content["findings"] = [
        {
            "id": "held-review-finding",
            "type": "reasoning",
            "text": "The acceptance check names no command.",
        }
    ]
    plan_review.store_plan_review(content)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming(session=AUTHOR_SESSION)
    _hold_request("fixture")
    _weigh_at_current_edit(path)

    report = _pass()

    assert [row["plan"] for row in report["composed"]] == [], report
    obligations = [row for row in report["obligations"] if row["plan"] == "fixture"]
    assert obligations, report
    obligation = obligations[0]
    assert obligation["session"] == AUTHOR_SESSION
    assert obligation["reason"] == "an unanswered finding"
    assert "held-review-finding" in obligation["finding_ids"]
    assert obligation["version"] == 3
    # It is durable, so the owning session's obligations view can read it.
    stored = plan_review._reflex_obligations("sample")
    assert "held-review-finding" in stored["fixture"]["finding_ids"]


def test_a_plan_with_no_held_request_is_passed_over_at_edit(
    two_section_plan, monkeypatch
):
    _home, _repo, path = two_section_plan
    launched = _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming(session=AUTHOR_SESSION)
    _weigh_at_current_edit(path)

    report = _pass()

    assert [row["plan"] for row in report["composed"]] == [], report
    assert report["read"] == [], report
    assert report["obligations"] == []
    assert launched == []
    assert plan_review._weighed_edits("sample").get("fixture") == path.stat().st_mtime
