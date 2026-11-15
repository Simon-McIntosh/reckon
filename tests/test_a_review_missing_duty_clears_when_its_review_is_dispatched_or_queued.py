"""A review that exists is never listed as a missing review.

A run whose review is dispatched, queued, or whose queued review was admitted
under a new run id owes the coordinator nothing: a review stands for the run's
head and the reflex composes no second one while it does. Listing
``review-missing`` in any of those windows read as an instruction to dispatch a
duplicate, and the listing carried the full dispatch command for it, so a
coordinator retyped a dispatch the lane refused by naming the review that
already existed for that head.

Each case reads both surfaces that publish this duty. The derivation is the
fleet as it reads now; the held answer is the row a station tick computed
before the fleet moved, which the stop hook settles against the pointer at read
time. Every case first shows the row exists at all — ``review-missing`` with a
dispatch command — then moves the fleet into the window and reads both surfaces
again, so a surface that settled nothing would show the stale row standing and
the command beside it.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, review as review_module, runs
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "review-exists-fixture"
SESSION = "coordinator-fixture"
NODE_ID = "reviewed-node"
RUN_ID = "run-awaiting-review"
QUEUED_REVIEW = "r-queued-review"
ADMITTED_REVIEW = "r-admitted-review"
DISPATCH_COMMAND = "reckon crew dispatch"
OBSERVED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project, its git tree and a crew home of its own."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed review-exists fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(json.dumps({PROJECT: str(root / "docs")}))
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 0}
    )
    return root


def _write_delivered_run(repository: Path, tmp_path: Path, *, head: str) -> None:
    """Record one complete run whose tree is at ``head`` with no review stored."""
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {NODE_ID}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": NODE_ID,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)


def _write_review_pointer(
    review_run_id: str,
    *,
    head: str,
    phase: str,
    queued_at: datetime | None = None,
) -> None:
    """Record one review run of the reviewed run at ``head``.

    The granted path is the head-keyed record the dispatch composes, so the
    review stands for that pair wherever its pointer says it is.
    """
    record = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "backend": "local",
        "role": "review",
        "node": {
            "id": "review-" + NODE_ID,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "role": "review",
            "time_budget": "20m",
            "write_paths": [
                str(review_module.review_path(PROJECT, RUN_ID, reviewed_head_sha=head))
            ],
        },
    }
    if queued_at is not None:
        record["queued_at"] = queued_at.isoformat()
    runs._write_json(runs.pointer_path(review_run_id), record)


def _record_dispatch(*, status: str, head: str, review_run_id: str) -> None:
    """Write onto the reviewed run's pointer what the reflex records for an attempt."""
    pointer = runs.read_pointer(RUN_ID)
    pointer["review_dispatch"] = {
        "status": status,
        "reason": "fixture",
        "run_id": review_run_id,
        "backend": "fixture-lane",
        "head": head,
        "at": OBSERVED_AT.isoformat(),
        "attempt": 1,
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)


def _derived_duties() -> list[dict[str, Any]]:
    """The duties the view derives from the fleet as it reads now."""
    return obligations_module.obligations(PROJECT, SESSION)["obligations"]


def _served_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows a reader is served from a held answer these rows were in."""
    payload = {
        "project": PROJECT,
        "session": SESSION,
        "obligations": rows,
        "acknowledged": [],
        "summary": {"count": len(rows), "oldest_age_seconds": 180},
    }
    return obligation_snapshot.without_departed_run_duties(payload)["obligations"]


def _review_duties(duties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """This run's duties, each with the command it lists."""
    listed = []
    for item in duties:
        if str(item.get("run_id") or "") != RUN_ID:
            continue
        listed.append(
            {
                "kind": str(item.get("kind") or ""),
                "next_command": str(item.get("next_command") or ""),
            }
        )
    return listed


def _assert_review_exists(duties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """No missing-review duty names this run, and no dispatch command is listed."""
    listed = _review_duties(duties)
    kinds = [duty["kind"] for duty in listed]
    assert "review-missing" not in kinds, listed
    for duty in listed:
        assert DISPATCH_COMMAND not in duty["next_command"], listed
    return listed


def test_a_dispatched_review_leaves_no_missing_review_duty(
    repository: Path, tmp_path: Path
) -> None:
    """A review working on the head is in flight, on both surfaces."""
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, head=head)

    held = _derived_duties()
    before = _review_duties(held)
    assert [duty["kind"] for duty in before] == ["review-missing"], before
    assert DISPATCH_COMMAND in before[0]["next_command"], before

    _write_review_pointer("r-dispatched-review", head=head, phase="working")
    _record_dispatch(
        status="dispatched", head=head, review_run_id="r-dispatched-review"
    )

    listed = _assert_review_exists(_derived_duties())
    assert [duty["kind"] for duty in listed] == ["review-in-flight"], listed
    assert _assert_review_exists(_served_rows(held)) == [], held


def test_a_queued_review_leaves_no_missing_review_duty(
    repository: Path, tmp_path: Path
) -> None:
    """A review run waiting in the lane's queue is queued, not missing."""
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, head=head)

    held = _derived_duties()
    before = _review_duties(held)
    assert [duty["kind"] for duty in before] == ["review-missing"], before
    assert DISPATCH_COMMAND in before[0]["next_command"], before

    _write_review_pointer(QUEUED_REVIEW, head=head, phase="queued", queued_at=OBSERVED_AT)
    _record_dispatch(status="queued", head=head, review_run_id=QUEUED_REVIEW)

    listed = _assert_review_exists(_derived_duties())
    for duty in listed:
        assert duty["kind"] in {"review-in-flight", "review-queued"}, listed
    assert _assert_review_exists(_served_rows(held)) == [], held


def test_a_queued_review_admitted_under_a_new_id_leaves_no_missing_review_duty(
    repository: Path, tmp_path: Path
) -> None:
    """The hand-over from a queued id to the admitted one keeps the review real."""
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(repository, tmp_path, head=head)

    held = _derived_duties()
    before = _review_duties(held)
    assert [duty["kind"] for duty in before] == ["review-missing"], before
    assert DISPATCH_COMMAND in before[0]["next_command"], before

    _write_review_pointer(QUEUED_REVIEW, head=head, phase="queued")
    _record_dispatch(status="queued", head=head, review_run_id=QUEUED_REVIEW)

    runs.pointer_path(QUEUED_REVIEW).unlink()
    _write_review_pointer(ADMITTED_REVIEW, head=head, phase="launching")
    recorded = runs.read_pointer(RUN_ID)["review_dispatch"]
    assert recorded["run_id"] == QUEUED_REVIEW, recorded

    assert _review_duties(_derived_duties()) == [], _derived_duties()
    assert _assert_review_exists(_served_rows(held)) == [], held