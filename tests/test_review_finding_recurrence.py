"""Both consecutive-review recurrence shares, computed from stored responses.

For every finding type in a window the plan-review fold reports two shares over
each plan's consecutive review pairs: the share of a type's findings whose
preceding review raised the same type, and the share whose preceding review
carries a same-type finding with a stored response of action ``acted``. The
second share is the figure a recurrence re-measure compares across windows, so
it must read the answer stored against the finding rather than the bare presence
of a same-type finding: a type raised and declined, or raised and left
unanswered, is a recurrence but not an act.

The fold is exercised through the review view as well as directly, because the
view is where a reader meets the figure; a fold the summary carries and the
view drops is invisible.

Every fixture is synthesised: a temporary repository, a temporary config home,
and committed review records under the repository's own ``docs/state`` tree. No
real store is read or written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight, mcp_views
from reckon.crew import plan_review

PROJECT = "finding-recurrence"
SLUG = "demo"
DEFAULT_THRESHOLD = 0.30

RUN_R1 = "r-20261005T100000000000-plan-review-of-demo"
RUN_R2 = "r-20261006T020000000000-plan-review-of-demo"
RUN_R3 = "r-20261006T030000000000-plan-review-of-demo"


def _review_config(threshold: float = DEFAULT_THRESHOLD) -> dict:
    return {"review": {"plan_change_threshold": threshold}}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A synthesised checkout, config home and mounts entry, isolated from the real ones."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    monkeypatch.setattr(
        flight, "resolve", lambda **kw: SimpleNamespace(config=_review_config())
    )
    return SimpleNamespace(home=home, repo=repo)


def _commit_record(repo: Path, record: dict) -> None:
    directory = (
        repo / "docs" / "state" / PROJECT / "reviews" / "plan" / record["plan_slug"]
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{record['review_run_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def _record(
    run_id: str,
    *,
    dispatched: str,
    findings: list,
    responses: dict | None = None,
) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": SLUG,
        "plan_version": 1,
        "review_run_id": run_id,
        "rubric": "content",
        "findings": findings,
        "responses": responses or {},
        "status": "ready",
        "timestamp": dispatched,
        "dispatched_at": dispatched,
    }


@pytest.fixture()
def committed(project):
    """Three reviews of one plan, exercising an acted, a declined and an unanswered recurrence.

    ``RUN_R1`` precedes the window and raises three types: ``dup`` answered
    ``acted``, ``shared_state`` answered ``declined``, and ``unanswered`` left
    with no response. ``RUN_R2`` recurs all three and raises ``fresh``, absent
    from every earlier review and from its predecessor ``RUN_R1``. ``RUN_R3``
    raises ``shared_state`` again, so a type recurs twice — once against a
    declined predecessor, once against an unanswered one — and no instance of it
    is an acted recurrence.
    """
    _commit_record(
        project.repo,
        _record(
            RUN_R1,
            dispatched="2026-10-05T10:00:00+00:00",
            findings=[
                {"id": "a1", "type": "dup", "text": "One owner."},
                {"id": "b1", "type": "shared_state", "text": "A shared field."},
                {"id": "c1", "type": "unanswered", "text": "Nobody answered."},
            ],
            responses={
                "a1": {
                    "action": "acted",
                    "reason": "",
                    "by": "author",
                    "when": "2026-10-05T10:05:00+00:00",
                },
                "b1": {
                    "action": "declined",
                    "reason": "the shared field is deliberate",
                    "by": "author",
                    "when": "2026-10-05T10:05:00+00:00",
                },
            },
        ),
    )
    _commit_record(
        project.repo,
        _record(
            RUN_R2,
            dispatched="2026-10-06T02:00:00+00:00",
            findings=[
                {"id": "a2", "type": "dup", "text": "Same owner again."},
                {"id": "b2", "type": "shared_state", "text": "Still shared."},
                {"id": "c2", "type": "unanswered", "text": "Still unanswered."},
                {"id": "d2", "type": "fresh", "text": "A new concern."},
            ],
        ),
    )
    _commit_record(
        project.repo,
        _record(
            RUN_R3,
            dispatched="2026-10-06T03:00:00+00:00",
            findings=[{"id": "b3", "type": "shared_state", "text": "Shared again."}],
        ),
    )
    return project


def _fold(window: tuple[str, str] = ("2026-10-06", "2026-10-07")) -> dict:
    summary = plan_review.review_day_summary(PROJECT, since=window[0], until=window[1])
    return summary["finding_type_recurrence"]


def test_both_shares_are_computed_per_type_from_stored_responses(committed) -> None:
    fold = _fold()
    # ``dup`` is raised in RUN_R2, whose predecessor raised it and answered
    # ``acted``: a full recurrence and a full acted recurrence.
    dup = fold["dup"]
    assert (dup["findings"], dup["any"], dup["acted"]) == (1, 1, 1)
    assert dup["any_share"] == pytest.approx(1.0)
    assert dup["acted_share"] == pytest.approx(1.0)
    # ``shared_state`` recurs twice — RUN_R2 against RUN_R1's declined
    # instance, RUN_R3 against RUN_R2's unanswered one — so both instances
    # recur but neither is an act.
    shared = fold["shared_state"]
    assert (shared["findings"], shared["any"], shared["acted"]) == (2, 2, 0)
    assert shared["any_share"] == pytest.approx(1.0)
    assert shared["acted_share"] == pytest.approx(0.0)
    # ``unanswered`` recurs once against an unanswered instance and is not an
    # act either; its share is one of one because it appears once in the window.
    unanswered = fold["unanswered"]
    assert (unanswered["findings"], unanswered["any"], unanswered["acted"]) == (1, 1, 0)
    assert unanswered["acted_share"] == pytest.approx(0.0)
    # ``fresh`` is absent from its predecessor, so neither share moves.
    fresh = fold["fresh"]
    assert (fresh["findings"], fresh["any"], fresh["acted"]) == (1, 0, 0)
    assert fresh["any_share"] == pytest.approx(0.0)
    assert fresh["acted_share"] == pytest.approx(0.0)


def test_a_declined_or_unanswered_recurrence_is_not_acted(committed) -> None:
    # The discriminating measure: reading any same-type finding as acted would
    # report 1.0 for both ``shared_state`` and ``unanswered``.
    fold = _fold()
    assert fold["shared_state"]["any_share"] == pytest.approx(1.0)
    assert fold["shared_state"]["acted_share"] == pytest.approx(0.0)
    assert fold["unanswered"]["acted_share"] == pytest.approx(0.0)


def test_a_predecessor_before_the_window_still_anchors_the_fold(committed) -> None:
    # RUN_R1 sits outside the window yet is the predecessor RUN_R2 recurs
    # against; including it in the window does not add its own findings, since a
    # plan's first review has nothing to recur against.
    assert _fold()["dup"]["any_share"] == pytest.approx(1.0)
    assert _fold(("2026-10-05", "2026-10-07"))["dup"]["findings"] == 1


def test_the_review_view_reports_both_shares(committed) -> None:
    view = mcp_views.serve_crew_view(
        "plan-review",
        project=PROJECT,
        since="2026-10-06T00:00:00+00:00",
        until="2026-10-07T00:00:00+00:00",
    )
    fold = view["day_summary"]["finding_type_recurrence"]
    assert fold == _fold()
    assert fold["dup"]["acted_share"] == pytest.approx(1.0)
    assert fold["shared_state"]["acted_share"] == pytest.approx(0.0)
