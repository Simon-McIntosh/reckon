"""Reflex review waits do not freeze the coordinator's next dispatch."""

from __future__ import annotations

import copy
import importlib
import os
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligations, recovery, review, runs
from reckon.crew.node import TaskNode
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
RUN = "r-delivered-for-reflex"


@pytest.fixture()
def delivered(admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811 - imported fixture
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
    yield pointer, config
    real_store_is_not_a_fixture_target(runs.crew_home())


def _publish(pointer: dict, *, age: int) -> None:
    stamp = time.time() - age
    os.utime(pointer["manifest_path"], (stamp, stamp))
    runs._write_json(runs.pointer_path(RUN), pointer)


def _dispatch(pointer: dict, config: dict) -> dict:
    return dispatch_module.dispatch(
        node=TaskNode(
            id="next-node",
            goal="record the next independent signal",
            plan="plan-a",
            section="dispatch",
            done_when="pytest tests/test_signal.py reports passed",
            write_paths=["next.txt"],
            time_budget="20m",
            spec_level="exact",
        ),
        project=PROJECT,
        repo=Path(pointer["repo"]),
        session=SESSION,
        config=config,
        launcher=_launcher([]),
    )


def test_a_session_waiting_inside_the_reflex_window_can_dispatch(delivered):
    pointer, config = delivered
    _publish(pointer, age=obligations._reflex_review_window_seconds() // 2)
    ownership_reads = []
    owns = obligations._reflex_owns_review

    def read_owner(project, record):
        ownership_reads.append(record["run_id"])
        return owns(project, record)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(obligations, "_reflex_owns_review", read_owner)
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            assert owns(PROJECT, pointer)
            assert recovery.classify_pointer(pointer)["classification"] == "scoring"
            result = _dispatch(pointer, config)

    assert result["run_id"] != RUN
    assert result["unreconciled_override"] is None
    assert RUN in ownership_reads, (
        "dispatch must consult the shared ownership predicate"
    )


@pytest.mark.parametrize("armed,expired", [(False, False), (True, True)])
def test_a_run_without_a_current_reflex_wait_is_dispatched_over_and_recorded(
    delivered, armed, expired
):
    pointer, config = delivered
    window = obligations._reflex_review_window_seconds()
    _publish(pointer, age=2 * window + 1 if expired else window // 2)
    with (
        runs.follower_claim(PROJECT, SESSION, delivery="stream")
        if armed
        else nullcontext(),
    ):
        result = _dispatch(pointer, config)
    assert result["run_id"] != RUN
    backlog = result["unreconciled_override"]
    assert backlog["requested"] is False
    assert [row["run_id"] for row in backlog["waived_runs"]] == [RUN]


def test_the_next_sweep_composes_a_review_after_twice_the_window(delivered):
    pointer, config = delivered
    _publish(pointer, age=2 * obligations._reflex_review_window_seconds() + 1)
    assert not recovery._review_in_flight(pointer)
    launched = []
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        report = recovery.dispatch_awaiting_reviews(
            project=PROJECT,
            session=SESSION,
            config=config,
            launcher=_launcher(launched),
        )
    assert len(launched) == 1, report
    assert len(report["dispatched"]) == 1, report
    recorded = runs.read_pointer(RUN)[recovery.REVIEW_DISPATCH_FIELD]
    assert recorded["status"] == "dispatched"
    assert recorded["run_id"] == report["dispatched"][0]
    head = git(Path(pointer["repo"]), "rev-parse", "HEAD")
    assert recorded["head"] == head
    reviewer = runs.read_pointer(recorded["run_id"])
    assert head in Path(reviewer["node"]["brief"]).read_text()


def test_a_refused_reflex_names_its_cause_without_promising_a_review(delivered):
    pointer, config = delivered
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review.REVIEW_DIMENSIONS
    )
    record = review.parse_review(emitted)
    record.update(
        project=PROJECT,
        reviewed_run_id=RUN,
        review_run_id="r-earlier-review",
        reviewed_base_sha=pointer["base_sha"],
        reviewed_head_sha=pointer["base_sha"],
        findings=[
            {
                "file": "reckon/signal.py",
                "line": "1",
                "text": "the signal guard needs an answer",
                "severity": review.BLOCKING_FINDING_SEVERITY,
            }
        ],
    )
    review.store_review(record)
    _publish(pointer, age=2 * obligations._reflex_review_window_seconds() + 1)
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        report = recovery.dispatch_awaiting_reviews(
            project=PROJECT,
            session=SESSION,
            config=config,
            launcher=lambda *a, **k: pytest.fail("a refused review must never launch"),
        )
        refused = report["refused"]
        assert len(refused) == 1, report
        current = runs.read_pointer(RUN)
        assert current[recovery.REVIEW_DISPATCH_FIELD]["status"] == "refused"
        row = recovery.classify_pointer(current)
        duty = obligations._review_duty_item(
            PROJECT,
            row,
            pointer=current,
            grace=1,
            now=datetime.now(UTC),
            live_pointers=[current],
            placements={},
        )
        assert "blocking finding" in duty["reason"]
        assert duty["reason"] == refused[0]["reason"]
        assert "crew dispose" in duty["next_command"]
        assert "review_owner" not in duty
        assert "composes the review" not in duty["next_command"]
