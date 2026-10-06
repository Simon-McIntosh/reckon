"""A review record can be written to the project's committed reviews tree.

A review is evidence about a plan or a run, so — beside the host staging store
where a live review is written for the gate to read — it also belongs in the
repository, under ``docs/state/<project>/reviews/``, where it travels with the
plan, the ledger and the evidence. These cases hold the two path owners to that
committed root beside an unchanged staging path, hold a committed record's
dispatch and completion times to the run record that produced it rather than to
the store clock, and hold an answered finding to an append-only event list while
the findings and the rest of the body keep their bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import plan_review
from reckon.crew import review as review_module

PROJECT = "committed-root"
REVIEWED_RUN = "r-reviewed-run"
REVIEW_RUN = "r-review-run"
PLAN_SLUG = "demo"
PLAN_VERSION = 3
BLOB = "a" * 40

DISPATCH_TS = "2026-10-06T09:00:00+00:00"
COMPLETION_TS = "2026-10-06T09:07:30+00:00"


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout with the state tree the ledger uses."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _run_record(checkout: Path, run_id: str) -> Path:
    """Write one run's committed per-run record beside the ledger."""
    path = checkout / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "dispatched_at": DISPATCH_TS,
                "completed_at": COMPLETION_TS,
                "gate": "passed",
            }
        ),
        encoding="utf-8",
    )
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── (1) Both path owners resolve the committed tree beside the staging path ──


def test_the_committed_root_is_the_projects_reviews_tree(checkout: Path) -> None:
    committed = review_module.committed_review_root(PROJECT, root=checkout)
    assert committed == checkout / "docs" / "state" / PROJECT / "reviews"


def test_review_path_resolves_the_run_directory_for_the_reviewed_run(
    checkout: Path, tmp_path: Path
) -> None:
    committed = review_module.committed_review_root(PROJECT, root=checkout)
    assert committed is not None

    committed_path = review_module.review_path(
        PROJECT, REVIEWED_RUN, committed_root=committed, review_run_id=REVIEW_RUN
    )
    # The run directory is named for the reviewed run; the file for the review.
    assert committed_path == committed / "run" / REVIEWED_RUN / f"{REVIEW_RUN}.json"

    # The staging path is unchanged: the same relative shape it always resolved.
    staging = tmp_path / "staging"
    assert (
        review_module.review_path(PROJECT, REVIEWED_RUN, base_dir=staging)
        == staging / PROJECT / f"{REVIEWED_RUN}.json"
    )
    assert (
        review_module.review_path(
            PROJECT, REVIEWED_RUN, base_dir=staging, reviewed_head_sha=BLOB
        )
        == staging / PROJECT / f"{REVIEWED_RUN}.at-{BLOB}.json"
    )


def test_plan_review_path_resolves_the_plan_directory_for_the_reviewed_plan(
    checkout: Path, tmp_path: Path
) -> None:
    committed = review_module.committed_review_root(PROJECT, root=checkout)
    assert committed is not None

    committed_path = plan_review.plan_review_path(
        PROJECT,
        PLAN_SLUG,
        PLAN_VERSION,
        committed_root=committed,
        review_run_id=REVIEW_RUN,
    )
    assert committed_path == committed / "plan" / PLAN_SLUG / f"{REVIEW_RUN}.json"

    # The staging path is byte-identical to the version-and-blob rule it always
    # used: the committed root is a second tree, not a second path rule.
    staging = tmp_path / "staging"
    assert (
        plan_review.plan_review_path(PROJECT, PLAN_SLUG, PLAN_VERSION, base_dir=staging)
        == staging / PROJECT / f"plan-{PLAN_SLUG}.v{PLAN_VERSION}.json"
    )
    assert (
        plan_review.plan_review_path(
            PROJECT, PLAN_SLUG, PLAN_VERSION, base_dir=staging, reviewed_blob_sha=BLOB
        )
        == staging / PROJECT / f"plan-{PLAN_SLUG}.v{PLAN_VERSION}.at-{BLOB[:8]}.json"
    )


def test_a_committed_plan_path_without_a_review_run_is_refused(
    checkout: Path,
) -> None:
    committed = review_module.committed_review_root(PROJECT, root=checkout)
    assert committed is not None
    with pytest.raises(ValueError, match="review run id"):
        plan_review.plan_review_path(
            PROJECT, PLAN_SLUG, PLAN_VERSION, committed_root=committed
        )


# ── (2) A committed record's times come from the run record ──────────────────


def test_a_committed_record_carries_the_run_records_times(checkout: Path) -> None:
    _run_record(checkout, REVIEW_RUN)
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "status": "parsed",
    }
    path = review_module.store_committed_review(record, root=checkout)

    assert path == (
        checkout
        / "docs"
        / "state"
        / PROJECT
        / "reviews"
        / "run"
        / REVIEWED_RUN
        / f"{REVIEW_RUN}.json"
    )
    stored = _read(path)
    # Dispatch and completion come from the run record, not the store clock.
    assert stored[review_module.DISPATCH_TIME_KEY] == DISPATCH_TS
    assert stored[review_module.COMPLETION_TIME_KEY] == COMPLETION_TS
    # The store time is the record's own storage stamp and is a distinct fact:
    # neither run time was defaulted to it.
    assert stored["timestamp"] not in (DISPATCH_TS, COMPLETION_TS)

    # A run record that carries no times leaves the keys off rather than
    # substituting the clock: an unrecorded time is never stored as a measured
    # one.
    _run_record(checkout, REVIEWED_RUN)
    (
        checkout / "docs" / "state" / PROJECT / "runs" / f"{REVIEWED_RUN}.json"
    ).write_text(
        json.dumps({"run_id": REVIEWED_RUN, "project": PROJECT}), encoding="utf-8"
    )
    bare = review_module.store_committed_review(
        {
            "project": PROJECT,
            "reviewed_run_id": REVIEWED_RUN,
            "review_run_id": REVIEWED_RUN,
        },
        root=checkout,
    )
    stored_bare = _read(bare)
    assert review_module.DISPATCH_TIME_KEY not in stored_bare
    assert review_module.COMPLETION_TIME_KEY not in stored_bare


# ── (3) An answer appends an event; the body keeps its bytes ─────────────────


def _finding(finding_id: str) -> dict:
    return {"id": finding_id, "type": "anchor-resolves", "text": "an advisory finding"}


def test_record_response_appends_events_and_keeps_the_body(tmp_path: Path) -> None:
    findings = [_finding("f1"), _finding("f2")]
    stored_path = plan_review.store_plan_review(
        {
            "project": PROJECT,
            "plan_slug": PLAN_SLUG,
            "plan_version": PLAN_VERSION,
            "review_run_id": REVIEW_RUN,
            "findings": findings,
            "responses": {},
            "status": "ready",
        },
        base_dir=tmp_path,
    )
    before = _read(stored_path)

    first = plan_review.record_response(before, "f1", action="acted", base_dir=tmp_path)
    second = plan_review.record_response(
        _read(first),
        "f1",
        action="declined",
        reason="house convention",
        base_dir=tmp_path,
    )
    third = plan_review.record_response(
        _read(second), "f2", action="acted", base_dir=tmp_path
    )
    stored = _read(third)

    # The latest answer per finding is the ``responses`` map, rewritten in place.
    assert stored["responses"]["f1"]["action"] == "declined"
    assert stored["responses"]["f2"]["action"] == "acted"

    # Every answer is preserved in the append-only event list, in order, so the
    # answer the map overwrote survives as the event it was.
    events = stored[plan_review.RESPONSE_EVENTS_KEY]
    assert [event["finding"] for event in events] == ["f1", "f1", "f2"]
    assert [event["action"] for event in events] == ["acted", "declined", "acted"]
    assert events[1]["reason"] == "house convention"

    # The findings and every other body field keep their bytes.
    assert stored["findings"] == before["findings"]
    for key, value in before.items():
        if key in ("responses", plan_review.RESPONSE_EVENTS_KEY):
            continue
        assert stored[key] == value
