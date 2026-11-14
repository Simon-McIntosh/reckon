"""A review refused on a claim is composed again once the claim is released.

The reflex records every review attempt on the run it acted for. When the
dispatch is refused because a live run still claims the review record the new
review would write, that record is the whole account of why no review started —
so on a later sweep, once the claiming run has gone, nothing anymore stands
between the run and its review. The reflex composes it again.

Three passes on one temporary crew home show the boundary: the first refuses
while an older review still holds the record claim, the second repeats that
refusal without dispatching, and the third — the claim released — dispatches.
The negative control holds the stored refusal as the reason on that third pass,
which is the defect this node repairs.
"""

from __future__ import annotations

import copy
import importlib
import os
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, review, runs
from reckon.crew.node import CrewError
from tests.conftest import real_store_is_not_a_fixture_target
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import (
    CONFIG,
    PROJECT,
    SESSION,
    _lane_document,
    _launcher,
    admit_repo,  # noqa: F401 - shared temporary repository fixture
)

dispatch_module = importlib.import_module("reckon.crew.dispatch")

RUN = "r-delivered-claim-clears"
OLDER_REVIEW = "r-older-review-holds-the-record"


@pytest.fixture()
def delivered(admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811 - imported fixture
    """A delivered run that owes a review, with an older review still live."""
    base = git(admit_repo, "rev-parse", "HEAD")
    source = admit_repo / "reckon" / "signal.py"
    source.parent.mkdir()
    source.write_text("SIGNAL = 1\n", encoding="utf-8")
    git(admit_repo, "add", "reckon/signal.py")
    git(
        admit_repo,
        "commit",
        "-qm",
        "test: record signal",
        "-m",
        "Provide runtime source for review.",
    )
    head = git(admit_repo, "rev-parse", "HEAD")
    manifest = runs.crew_home() / "manifests" / f"{RUN}.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f"node: delivered\nstatus: complete\ncommits: [{head}]\n")
    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(admit_repo),
        "worktree": str(admit_repo),
        "base_sha": base,
        "backend": "clive",
        "launch": "cli",
        "phase": "complete",
        "process_alive": False,
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": "delivered",
            "plan": "plan-a",
            "section": "dispatch",
            "time_budget": "20m",
            "write_paths": ["reckon/signal.py"],
        },
    }
    manifest_stamp = time.time() - 30
    os.utime(manifest, (manifest_stamp, manifest_stamp))
    config = copy.deepcopy(CONFIG)
    config["roles"]["review"] = {}
    config["fences"]["unreconciled_run_grace"] = "1s"
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=10)
    )
    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        lambda *a, **k: {"path": str(admit_repo), "base": head, "base_sha": head},
    )
    yield pointer, config, admit_repo, head, base
    real_store_is_not_a_fixture_target(runs.crew_home())


def _publish(pointer: dict) -> None:
    runs._write_json(runs.pointer_path(RUN), pointer)


def _older_review_pointer(repository: Path, reviewed_head: str) -> dict:
    """A live review of an older revision, holding this run's record claim."""
    record_paths = review.run_review_record_paths(
        runs.run_dir(RUN), RUN, reviewed_head_sha=reviewed_head
    )
    return {
        "run_id": OLDER_REVIEW,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "phase": "working",
        "process_alive": True,
        "pid": 0,
        "role": "review",
        "node": {
            "id": "review-of-delivered-at-the-older-head",
            "plan": "plan-a",
            "section": "dispatch",
            "time_budget": "20m",
            "write_paths": record_paths,
        },
    }


def _pass(pointer: dict, config: dict, recorded: list) -> dict:
    # Re-read first: the reflex records each attempt on the pointer, and a pass
    # that republished its stale copy would erase the record it is testing.
    try:
        current = runs.read_pointer(RUN)
    except CrewError:
        current = None
    _publish(current or pointer)
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT,
        session=SESSION,
        config=config,
        launcher=_launcher(recorded),
    )


def test_a_claim_refused_review_is_composed_again_when_the_claim_clears(
    delivered,
) -> None:
    pointer, config, repository, _head, base = delivered
    older = _older_review_pointer(repository, base)
    runs._write_json(runs.pointer_path(OLDER_REVIEW), older)
    recorded: list = []

    first = _pass(pointer, config, recorded)

    refusals = first.get("refused") or []
    assert refusals, first
    assert "live claim" in str(refusals[0].get("reason") or ""), refusals[0]
    assert not first.get("dispatched"), first
    recorded_claim = runs.read_pointer(RUN)["review_dispatch"]["claim"]
    assert recorded_claim == {"run_id": OLDER_REVIEW, "path": recorded_claim["path"]}
    attempts = runs.read_pointer(RUN)["review_dispatch"]["attempt"]

    second = _pass(pointer, config, recorded)

    # The claim still stands, so the reflex composes nothing and records no
    # second refusal: a rejection every sweep is the loop this fix removes.
    assert not second.get("dispatched"), second
    assert not second.get("refused"), second
    assert [
        report for report in second.get("reports") or () if report.get("held_claim")
    ], second
    assert runs.read_pointer(RUN)["review_dispatch"]["attempt"] == attempts
    assert len(recorded) == 0

    runs.pointer_path(OLDER_REVIEW).unlink()

    third = _pass(pointer, config, recorded)

    assert third.get("dispatched"), third
    assert len(recorded) == 1
