"""A run review is filed under the run it reviewed, whatever plan that run carried.

A run review records both the run it reviewed and the plan that run was carried
under. The plan is context, not the review's subject, so the committed store
files the record under ``run/<reviewed-run-id>/`` — named for the reviewed run —
and never under the plan tree, even though the body carries the run's
``plan_slug`` and, being a run, no ``plan_version``. The routing tests the
reviewed run before the plan, matching ``names_a_review_subject`` and the host
import's classifier, so a run review of a plan-carried run lands where a run
review belongs rather than in the plan tree at version 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import review as review_module

PROJECT = "run-tree"
REVIEWED_RUN = "r-reviewed-deployment"
REVIEW_RUN = "r-20261007T094345378621-review-of-eqdbms-layout-probe"
RUN_PLAN_SLUG = "jt60sa-discovery-completion"
PLAN_SLUG = "demo-plan"
PLAN_VERSION = 5


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout with the state tree the ledger uses."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_run_review_with_its_runs_plan_slug_lands_in_the_run_tree(
    checkout: Path,
) -> None:
    """A run review's plan_slug does not route it to the plan tree."""
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        # The body names the plan the reviewed run was carried under, and no
        # plan_version, because a run review's subject is the run, not the plan.
        "plan_slug": RUN_PLAN_SLUG,
        "plan_version": None,
        "scores": {"evidence": 17},
    }
    # names_a_review_subject accepts the record: the reviewed run is tested
    # first, so a plan slug with no integer version is context, not a refusal.
    assert review_module.names_a_review_subject(record)

    path = review_module.store_committed_review(record, root=checkout)

    committed = checkout / "docs" / "state" / PROJECT / "reviews"
    # Filed under the reviewed run's directory, named for the review run.
    assert path == committed / "run" / REVIEWED_RUN / f"{REVIEW_RUN}.json"
    # And nowhere under the plan tree, at version 0 or otherwise.
    assert not (committed / "plan" / RUN_PLAN_SLUG).exists()
    stored = _read(path)
    assert stored["reviewed_run_id"] == REVIEWED_RUN
    assert stored["plan_slug"] == RUN_PLAN_SLUG


def test_a_plan_review_still_lands_in_the_plan_tree(checkout: Path) -> None:
    """A plan review keeps routing to ``plan/<slug>/``."""
    record = {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": PLAN_VERSION,
        "review_run_id": REVIEW_RUN,
        "scores": {"evidence": 17},
    }
    path = review_module.store_committed_review(record, root=checkout)
    committed = checkout / "docs" / "state" / PROJECT / "reviews"
    assert path == committed / "plan" / PLAN_SLUG / f"{REVIEW_RUN}.json"
    assert not (committed / "run" / PLAN_SLUG).exists()
