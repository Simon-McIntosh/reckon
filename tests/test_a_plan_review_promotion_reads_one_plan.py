"""A plan's delivered reports are matched against that plan's stored reviews.

A promotion asks which of one plan's delivered reports the store already holds,
and answering that needs the review-run ids the store carries for that plan.
The ids were read for the whole project, so one plan's promotion listed every
plan's stored reviews. The cases below fill a temporary store with reviews of
two plans and hold the read to the plan it was asked about: the other plan's
record is never opened, a report already stored for this plan is still marked
stored, and one that is not stored is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import plan_review

PROJECT = "plan-scope-fixture"
HERE = "plan-scope-here"
OTHER = "plan-scope-other"
STORED_RUN = f"r-20261107T120000000000-plan-review-of-{HERE}"
UNSTORED_RUN = f"r-20261107T121000000000-plan-review-of-{HERE}"
OTHER_RUN = f"r-20261107T120000000000-plan-review-of-{OTHER}"


def _store(project: str, plan_slug: str, run_id: str) -> Path:
    return plan_review.store_plan_review(
        {
            "project": project,
            "plan_slug": plan_slug,
            "plan_version": 1,
            "review_run_id": run_id,
            "reviewed_blob_sha": "a" * 40,
            "rubric": "content",
        }
    )


def _deliver(project: str, plan_slug: str, run_id: str) -> Path:
    directory = plan_review.review_report_directory(project, plan_slug, run_id)
    directory.mkdir(parents=True)
    report_path = directory / "report.md"
    report_path.write_text(
        "RUBRIC reuse_search: pass — the plan reuses the existing owner.\n",
        encoding="utf-8",
    )
    (directory / "plan-review.json").write_text(
        json.dumps(
            {
                "project": project,
                "plan_slug": plan_slug,
                "plan_version": 1,
                "review_run_id": run_id,
                "report_path": str(report_path),
            }
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture()
def two_plan_store(isolated_reckon_home: Path) -> Path:
    """A temporary store holding reviews of two plans, one of them delivered."""
    _store(PROJECT, HERE, STORED_RUN)
    _store(PROJECT, OTHER, OTHER_RUN)
    _deliver(PROJECT, HERE, STORED_RUN)
    _deliver(PROJECT, HERE, UNSTORED_RUN)
    return isolated_reckon_home


def test_one_plans_delivery_opens_no_other_plans_review_record(
    two_plan_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    load = plan_review._load

    def recording_load(path: Path):
        opened.append(Path(path))
        return load(path)

    monkeypatch.setattr(plan_review, "_load", recording_load)

    reports_root = plan_review._plan_review_report_root(PROJECT, HERE)
    delivered = plan_review.delivered_reports(PROJECT, HERE)

    assert sorted(entry["review_run_id"] for entry in delivered) == sorted(
        [UNSTORED_RUN, STORED_RUN]
    )
    # The other plan's stored record is never opened. This plan's own record is,
    # because that is where the stored-id read answers from.
    store_root = plan_review._review_store.review_store_root()
    opened_records = [path for path in opened if path.is_relative_to(store_root)]
    assert opened_records, "the stored-id read opened no record at all"
    assert [path for path in opened if OTHER in path.name] == []
    assert all(HERE in path.name for path in opened_records)
    assert all(
        path.is_relative_to(reports_root)
        for path in opened
        if path not in opened_records
    )


def test_a_report_already_stored_for_this_plan_is_marked_stored(
    two_plan_store: Path,
) -> None:
    delivered = {
        entry["review_run_id"]: entry
        for entry in plan_review.delivered_reports(PROJECT, HERE)
    }

    assert delivered[STORED_RUN]["stored"] is True
    assert delivered[UNSTORED_RUN]["stored"] is False
