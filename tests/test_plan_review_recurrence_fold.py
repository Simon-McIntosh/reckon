"""The day summary carries a per-finding-type recurrence fold.

The fold answers, for each finding type in a window, two shares over the
consecutive review pairs of every plan: the share of a type's findings whose
preceding review raised the same type, and the share whose preceding review
carries a same-type finding that was answered ``acted``. It is the same
consecutive-pair walk the per-section fold uses, so a type's recurrence and the
change behind it are read off one ordering. The acted share is the one that must
read the stored response: a type raised and declined, or raised and left
unanswered, is not an act.

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

from reckon import flight
from reckon.crew import plan_review

PROJECT = "recurrence-fold"
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


def _commit_record(repo: Path, slug: str, record: dict) -> None:
    directory = repo / "docs" / "state" / PROJECT / "reviews" / "plan" / slug
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
    rubric: str = "content",
) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": SLUG,
        "plan_version": 1,
        "review_run_id": run_id,
        "rubric": rubric,
        "findings": findings,
        "responses": responses or {},
        "status": "ready",
        "timestamp": dispatched,
        "dispatched_at": dispatched,
    }


@pytest.fixture()
def committed(project):
    """A plan whose three reviews exercise a recurred-acted, recurred-unacted and fresh type.

    ``RUN_R1`` is dispatched before the window and raises ``dup`` with an
    ``acted`` response, so its successor ``RUN_R2`` recurs an acted type and
    raises ``shared_state`` declined. ``RUN_R3`` raises ``shared_state`` again —
    it recurs a type its predecessor carried but did not act — and a ``fresh``
    type absent from every earlier review.
    """
    repo = project.repo
    _commit_record(
        repo,
        SLUG,
        _record(
            RUN_R1,
            dispatched="2026-10-05T10:00:00+00:00",
            rubric="design",
            findings=[{"id": "a1", "type": "dup", "text": "One owner."}],
            responses={
                "a1": {
                    "action": "acted",
                    "reason": "",
                    "by": "author",
                    "when": "2026-10-05T10:05:00+00:00",
                }
            },
        ),
    )
    _commit_record(
        repo,
        SLUG,
        _record(
            RUN_R2,
            dispatched="2026-10-06T02:00:00+00:00",
            findings=[
                {"id": "a2", "type": "dup", "text": "Same owner again."},
                {"id": "b2", "type": "shared_state", "text": "A shared field."},
            ],
            responses={
                "b2": {
                    "action": "declined",
                    "reason": "the shared field is deliberate",
                    "by": "author",
                    "when": "2026-10-06T02:05:00+00:00",
                }
            },
        ),
    )
    _commit_record(
        repo,
        SLUG,
        _record(
            RUN_R3,
            dispatched="2026-10-06T03:00:00+00:00",
            findings=[
                {"id": "b3", "type": "shared_state", "text": "Still shared."},
                {"id": "c3", "type": "fresh", "text": "A new concern."},
            ],
        ),
    )
    return project


def _fold(window: tuple[str, str] = ("2026-10-06", "2026-10-07")) -> dict:
    summary = plan_review.review_day_summary(PROJECT, since=window[0], until=window[1])
    return summary["finding_type_recurrence"]


def test_the_fold_gives_the_any_share_beside_the_acted_share(committed) -> None:
    fold = _fold()
    # ``dup`` is raised in RUN_R2, whose predecessor RUN_R1 raised it and acted.
    dup = fold["dup"]
    assert dup["findings"] == 1
    assert dup["any"] == 1
    assert dup["acted"] == 1
    assert dup["any_share"] == pytest.approx(1.0)
    assert dup["acted_share"] == pytest.approx(1.0)
    # ``shared_state`` is raised twice: in RUN_R2 (fresh to RUN_R1) and in
    # RUN_R3, whose predecessor RUN_R2 raised it but declined it — so one of the
    # two recurs and none was acted.
    shared = fold["shared_state"]
    assert shared["findings"] == 2
    assert shared["any"] == 1
    assert shared["acted"] == 0
    assert shared["any_share"] == pytest.approx(0.5)
    assert shared["acted_share"] == pytest.approx(0.0)
    # ``fresh`` is absent from every preceding review, so neither share moves.
    fresh = fold["fresh"]
    assert fresh["findings"] == 1
    assert fresh["any"] == 0
    assert fresh["acted"] == 0
    assert fresh["any_share"] == pytest.approx(0.0)
    assert fresh["acted_share"] == pytest.approx(0.0)


def test_a_declined_same_type_is_not_counted_as_acted(committed) -> None:
    # The discriminating measure: RUN_R2 raised ``shared_state`` and declined it,
    # so RUN_R3's ``shared_state`` recurs (any) but is not an acted recurrence.
    # A fold that read any same-type finding as acted would report 0.5 here.
    fold = _fold()
    assert fold["shared_state"]["any_share"] == pytest.approx(0.5)
    assert fold["shared_state"]["acted_share"] == pytest.approx(0.0)


def test_a_predecessor_before_the_window_still_anchors_the_recurrence(
    committed,
) -> None:
    # RUN_R1 is dispatched on 5 October, outside the window, yet it is the
    # predecessor RUN_R2 recurs against, so ``dup`` reads as fully recurred.
    fold = _fold()
    assert fold["dup"]["any_share"] == pytest.approx(1.0)


def test_the_first_review_of_a_plan_has_nothing_to_recur_against(committed) -> None:
    # Widening the window to include RUN_R1 does not add its own ``dup`` finding:
    # a review with no predecessor is excluded rather than counted as fresh.
    fold = _fold(("2026-10-05", "2026-10-07"))
    assert fold["dup"]["findings"] == 1
