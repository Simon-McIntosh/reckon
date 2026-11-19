"""A promotion that succeeded is recorded as promoted.

A promotion removes the run's live pointer and writes its row into the run
store. The request that asked for it is closed afterwards, and closing it used
to read the run's commit list from the location the ledger write returned:
that location is the store, a database, so the read raised
``UnicodeDecodeError`` on its header, and a run file the checkout no longer
keeps raised a missing-file error. Either way the promotion was recorded as
failed after the promotion itself had completed.

Measured here: two runs whose records are held in the run store and not in the
checkout -- a review run with no commit and a read-only implementation run with
no commit -- each settle in the promoted phase with no error, their request
files are removed and their pointers are not marked failed. A fault forced
after the promotion completed leaves that recorded outcome unchanged. A fault
forced inside the promotion records the module, function and line that raised
beside the message.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from reckon import run_store
from reckon.crew import promotion_requests as requests
from reckon.crew import runs
from tests.helpers.git import git

PROJECT = "promoted-settles-fixture"
SESSION = "promoted-settles-session"
REVIEW_RUN_ID = "r-20261119T180000000001-review-of-a-delivery"
IMPLEMENT_RUN_ID = "r-20261119T180000000002-a-read-only-delivery"


@pytest.fixture()
def repository(tmp_path: Path, isolated_reckon_home: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "chore: seed the promotion fixture"),
    ):
        git(root, *arguments)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _store_run(run_id: str, *, role: str) -> None:
    """Hold one promoted run's record in the run store and nowhere else."""
    run_store.append(
        PROJECT,
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "role": role,
            "plan": "a-plan",
            "node": f"node-{run_id}",
            "backend": "native",
            "dispatched_at": "2026-11-19T17:00:00+00:00",
            "completed_at": "2026-11-19T18:00:00+00:00",
        },
    )
    assert run_store.stored_run_rows(PROJECT, run_ids=[run_id]), (
        "the fixture must hold the run in the store before the promotion reads it"
    )


def _plant_pointer(repository: Path, run_id: str, *, role: str) -> dict:
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "phase": "complete",
        "role": role,
        "launch": "in-harness",
        "created_at": "2026-11-19T17:00:00+00:00",
        "node": {"id": f"node-{run_id}", "role": role, "write_paths": []},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _promoted_result(run_id: str) -> dict:
    """What a completed promotion returns for a run with no commit.

    ``ledger.path`` names the store, exactly as a retired ledger's append
    returns it, and the record carries no commit list, so the request's commit
    lookup has only the store to read.
    """
    return {
        "run_id": run_id,
        "project": PROJECT,
        "accepted": True,
        "promoted": True,
        "record": {"run_id": run_id, "promoted_by": "fixture"},
        "ledger": {"path": str(run_store.store_path())},
    }


def _drive(run_id: str, execute) -> dict:
    request = requests.request_promotion(run_id, kind="complete")
    assert request["phase"] == requests.REQUESTED, request
    service = requests.PromotionService(execute=execute)
    launched = service.tick()
    assert launched == [run_id], launched
    service.settle(timeout=60)
    return requests.read_promotion(run_id)


def _assert_promoted(run_id: str, receipt: dict) -> None:
    assert receipt["phase"] == requests.PROMOTED, receipt
    assert not receipt.get("error"), receipt
    assert receipt["commits"] == [], receipt
    assert not requests._request_path(run_id).exists(), "the request must be removed"
    pointer = runs.read_pointer(run_id)
    assert pointer["promotion_status"] == requests.PROMOTED, pointer
    assert pointer["promotion_status"] != requests.FAILED, pointer


def test_a_promoted_review_run_with_no_commit_settles_as_promoted(repository):
    _store_run(REVIEW_RUN_ID, role="review")
    _plant_pointer(repository, REVIEW_RUN_ID, role="review")

    receipt = _drive(REVIEW_RUN_ID, lambda request: _promoted_result(REVIEW_RUN_ID))

    _assert_promoted(REVIEW_RUN_ID, receipt)


def test_a_promoted_read_only_implementation_run_settles_as_promoted(repository):
    _store_run(IMPLEMENT_RUN_ID, role="implement")
    _plant_pointer(repository, IMPLEMENT_RUN_ID, role="implement")

    receipt = _drive(
        IMPLEMENT_RUN_ID, lambda request: _promoted_result(IMPLEMENT_RUN_ID)
    )

    _assert_promoted(IMPLEMENT_RUN_ID, receipt)


def test_a_fault_after_the_promotion_completed_keeps_the_promoted_outcome(
    repository, monkeypatch
):
    _store_run(REVIEW_RUN_ID, role="review")
    _plant_pointer(repository, REVIEW_RUN_ID, role="review")

    def closing_the_request_failed(receipt):
        raise RuntimeError("closing the request failed")

    monkeypatch.setattr(requests, "_record_drained", closing_the_request_failed)

    receipt = _drive(REVIEW_RUN_ID, lambda request: _promoted_result(REVIEW_RUN_ID))

    assert receipt["phase"] == requests.PROMOTED, receipt
    assert not receipt.get("error"), receipt
    assert not requests._request_path(REVIEW_RUN_ID).exists()
    pointer = runs.read_pointer(REVIEW_RUN_ID)
    assert pointer["promotion_status"] == requests.PROMOTED, pointer


def _fail_inside_the_promotion() -> None:
    raise RuntimeError("the promotion's own step failed")


def _failure_site() -> tuple[str, str, int]:
    source, start = inspect.getsourcelines(_fail_inside_the_promotion)
    for offset, line in enumerate(source):
        if line.lstrip().startswith("raise "):
            return (__name__, _fail_inside_the_promotion.__name__, start + offset)
    raise AssertionError("the fixture must raise somewhere below")


def test_a_fault_inside_the_promotion_names_the_raising_site(repository):
    _store_run(IMPLEMENT_RUN_ID, role="implement")
    _plant_pointer(repository, IMPLEMENT_RUN_ID, role="implement")
    module, function, line = _failure_site()

    def failing(request):
        _fail_inside_the_promotion()

    receipt = _drive(IMPLEMENT_RUN_ID, failing)

    assert receipt["phase"] == requests.FAILED, receipt
    error = str(receipt.get("error") or "")
    assert "the promotion's own step failed" in error, error
    assert f"{module}:{function}:{line}" in error, error
