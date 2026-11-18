"""Resume deferrals end at judgment; explicit deferrals keep their owner."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import pending_landing, queue_landing, resume, runs
from tests.helpers.git import git
from tests.test_a_coordinator_holds_a_delivered_run import (
    RUN,
    _store_review,
)
from tests.test_a_coordinator_holds_a_delivered_run import (
    delivery as delivery_fixture,
)
from tests.test_a_merged_promoted_run_is_released import (
    PROJECT as MERGED_PROJECT,
)
from tests.test_a_merged_promoted_run_is_released import (
    cohort as cohort_fixture,
)

delivery = delivery_fixture
cohort = cohort_fixture


def _begin_resume() -> tuple[Path, int]:
    resume.prepare_resume(RUN)
    pointer = runs.read_pointer(RUN)
    manifest = Path(pointer["manifest_path"])
    manifest.write_text(
        manifest.read_text().replace("status: complete", "status: in-progress")
    )
    baseline = manifest.stat().st_mtime_ns
    stamp = datetime.now(UTC).isoformat()
    pointer.update(
        attempt=2,
        attempt_kind="resume",
        attempt_started_at=stamp,
        manifest_baseline_mtime_ns=baseline,
        resume_superseded_head={
            "head": resume.delivery_hold(pointer)["head"],
            "at": stamp,
        },
    )
    runs._write_json(runs.pointer_path(RUN), pointer)
    return manifest, baseline


def _deliver(manifest: Path, baseline: int, status: str = "complete") -> None:
    manifest.write_text(
        manifest.read_text().replace("status: in-progress", f"status: {status}")
    )
    written = max(manifest.stat().st_mtime_ns, baseline + 1_000_000)
    os.utime(manifest, ns=(written, written))


def _recorded_hold(run_id: str) -> dict:
    return json.loads((runs.run_dir(run_id) / resume.HOLD_RECORD_NAME).read_text())[
        resume.HOLD_FIELD
    ]


def test_reviewed_clean_resume_is_released_and_landed(delivery) -> None:
    manifest, baseline = _begin_resume()
    _deliver(manifest, baseline)
    _store_review(base=delivery["base"], head=delivery["head"])

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert resume.delivery_hold(runs.read_pointer(RUN)) is None
    recorded = _recorded_hold(RUN)
    assert recorded["released_at"]
    assert recorded["release_reason"]
    assert recorded["released_by"]


@pytest.mark.parametrize("status", ["complete", "failed", "blocked"])
def test_fresh_terminal_manifest_releases_the_resume_hold(delivery, status) -> None:
    manifest, baseline = _begin_resume()
    _deliver(manifest, baseline, status)

    pending_landing.finish_pending_landings()

    assert resume.delivery_hold(runs.read_pointer(RUN)) is None
    assert status in _recorded_hold(RUN)["release_reason"]


def test_review_of_the_resumed_attempt_releases_without_a_terminal_manifest(
    delivery,
) -> None:
    _begin_resume()
    _store_review(base=delivery["base"], head=delivery["head"])

    pending_landing.finish_pending_landings()

    assert resume.delivery_hold(runs.read_pointer(RUN)) is None
    assert "review" in _recorded_hold(RUN)["release_reason"]


def test_prior_review_and_stale_manifest_do_not_judge_the_resume(delivery) -> None:
    manifest, baseline = _begin_resume()
    _deliver(manifest, baseline)
    os.utime(manifest, ns=(baseline, baseline))

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert resume.delivery_hold(runs.read_pointer(RUN)) is not None
    assert not _recorded_hold(RUN).get("released_at")


def test_a_running_resume_keeps_its_hold_even_with_a_fresh_verdict(delivery) -> None:
    manifest, baseline = _begin_resume()
    _deliver(manifest, baseline)
    pointer = runs.read_pointer(RUN)
    pointer["pid"] = os.getpid()
    runs._write_json(runs.pointer_path(RUN), pointer)
    assert runs.record_process_alive(pointer) is True

    pending_landing.finish_pending_landings()

    assert resume.delivery_hold(runs.read_pointer(RUN)) is not None


def test_a_pointer_changed_after_judgment_keeps_its_hold(delivery) -> None:
    from reckon.crew.node import CrewError

    _begin_resume()
    judged = runs.read_pointer(RUN)
    changed = {**judged, "attempt": 3}
    runs._write_json(runs.pointer_path(RUN), changed)

    with pytest.raises(CrewError, match="changed before its hold"):
        resume.release_hold(RUN, "the attempt was judged", expected=judged)

    assert runs.read_pointer(RUN) == changed
    assert not _recorded_hold(RUN).get("released_at")


def test_a_review_of_another_head_cannot_release_the_resume(delivery) -> None:
    _begin_resume()
    _store_review(base=delivery["base"], head=delivery["base"])

    pending_landing.finish_pending_landings()

    assert resume.delivery_hold(runs.read_pointer(RUN)) is not None


def test_an_explicit_coordinator_hold_survives_terminal_delivery(delivery) -> None:
    resume.hold_delivery(RUN, "await a decision")

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert resume.delivery_hold(runs.read_pointer(RUN))["reason"] == "await a decision"
    assert any(row.get("status") == "held" for row in result["skipped"]), result


@pytest.mark.parametrize("explicit", [False, True])
def test_a_promoted_landed_run_retires_whatever_hold_it_carries(
    cohort, explicit
) -> None:
    member = cohort["members"]["clean"]
    run_id = member["pointer"]["run_id"]
    resume.hold_delivery(
        run_id, "await judgment", head="" if explicit else member["head"]
    )

    result = pending_landing.finish_pending_landings(only_run=run_id)

    assert not runs.pointer_path(run_id).exists(), result
    assert not member["tree"].exists(), result
    row = json.loads(
        ledger.run_path(MERGED_PROJECT, run_id, cohort["repo"]).read_text()
    )
    assert row["release"]["worktree_released"] is True


@pytest.mark.parametrize("name", ["dirty", "unresolved"])
def test_a_hold_does_not_bypass_workspace_evidence(cohort, name) -> None:
    member = cohort["members"][name]
    run_id = member["pointer"]["run_id"]
    resume.hold_delivery(run_id, "await judgment", head=member["head"])

    result = pending_landing.finish_pending_landings(only_run=run_id)

    assert runs.pointer_path(run_id).exists()
    assert member["tree"].exists()
    assert any(row["status"] == "release-refused" for row in result), result


@pytest.mark.parametrize("snapshot", [False, True])
def test_manual_release_attempts_pending_landing_on_the_next_pass(
    cohort, snapshot
) -> None:
    member = cohort["members"]["unmerged"]
    run_id = member["pointer"]["run_id"]
    resume.hold_delivery(run_id, "await a decision")
    pointer = runs.read_pointer(run_id)
    pending_landing.write_pending(
        run_id,
        MERGED_PROJECT,
        head=member["head"],
        pointer=pointer if snapshot else None,
    )
    attempts = []

    def land(repository, *, head, **kwargs):
        attempts.append(head)
        return {"commit": head}

    held = pending_landing.finish_pending_landings(only_run=run_id, land=land)
    assert not attempts, held
    assert any(row["status"] == "held" for row in held), held

    resume.release_hold(run_id, "the decision arrived")
    result = pending_landing.finish_pending_landings(only_run=run_id, land=land)

    assert attempts == [member["head"]], result
    assert any(row["status"] == "finished" for row in result), result


def test_one_pass_judges_a_mixed_cohort(delivery, tmp_path) -> None:
    pointer = runs.read_pointer(RUN)
    delivery_tree = tmp_path / "delivery-tree"
    git(
        delivery["repo"],
        "worktree",
        "add",
        "--detach",
        str(delivery_tree),
        delivery["head"],
    )
    pointer["worktree"] = str(delivery_tree)
    runs._write_json(runs.pointer_path(RUN), pointer)
    manifest, baseline = _begin_resume()
    _deliver(manifest, baseline)
    _store_review(base=delivery["base"], head=delivery["head"])
    original = runs.read_pointer(RUN)
    for name, running in (("running", True), ("explicit", False)):
        run_id = f"r-cohort-{name}"
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True)
        member_manifest = directory / "manifest.md"
        member_manifest.write_text(manifest.read_text())
        pointer = {
            **original,
            "run_id": run_id,
            "manifest_path": str(member_manifest),
            "node": {**original["node"], "id": name},
        }
        if running:
            pointer["pid"] = os.getpid()
            member_manifest.write_text(
                member_manifest.read_text().replace(
                    "status: complete", "status: in-progress"
                )
            )
        else:
            pointer.pop("delivery_hold")
        runs._write_json(runs.pointer_path(run_id), pointer)
        if not running:
            resume.hold_delivery(run_id, "the coordinator owns this decision")

    repository = delivery["repo"]
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", str(origin))
    git(repository, "remote", "add", "origin", str(origin))
    git(repository, "push", "-q", "origin", "main")
    tree = tmp_path / "integrated-tree"
    git(repository, "worktree", "add", "--detach", str(tree), delivery["head"])
    promoted_id = "r-cohort-integrated"
    directory = runs.run_dir(promoted_id)
    directory.mkdir(parents=True)
    promoted_manifest = directory / "manifest.md"
    promoted_manifest.write_text(f'status: complete\ncommits: ["{delivery["head"]}"]\n')
    promoted = {
        "run_id": promoted_id,
        "project": original["project"],
        "repo": str(repository),
        "worktree": str(tree),
        "manifest_path": str(promoted_manifest),
        "base_sha": delivery["base"],
        "phase": "complete",
        "role": "implement",
        "launch": "in-harness",
        "node": {"id": "integrated", "write_paths": []},
    }
    runs._write_json(runs.pointer_path(promoted_id), promoted)
    (repository / "docs" / "state" / original["project"] / "runs").mkdir()
    ledger.write_run_file(
        original["project"],
        {
            **promoted,
            "commits": [delivery["head"]],
            "promoted_revision": delivery["head"],
            "gate": "passed",
        },
        repository,
    )
    resume.hold_delivery(
        promoted_id, "a resume deferred this head", head=delivery["head"]
    )

    base_tree = tmp_path / "base-tree"
    git(repository, "worktree", "add", "--detach", str(base_tree), delivery["base"])
    try:
        result = queue_landing.land_once()
    finally:
        git(repository, "worktree", "remove", str(base_tree))

    assert result["landed"] == [RUN], result
    assert resume.delivery_hold(runs.read_pointer(RUN)) is None
    for name in ("running", "explicit"):
        assert resume.delivery_hold(runs.read_pointer(f"r-cohort-{name}")) is not None
    assert not runs.pointer_path(promoted_id).exists(), result
    assert not tree.exists(), result
    row = json.loads(
        ledger.run_path(original["project"], promoted_id, repository).read_text()
    )
    assert row["release"]["worktree_released"] is True


@pytest.mark.parametrize("mark", ["missing", "matching", "phase-rewritten"])
def test_a_marked_integrated_run_without_a_journal_records_its_release(
    cohort, mark
) -> None:
    member = cohort["members"]["clean"]
    run_id = member["pointer"]["run_id"]
    pointer = runs.read_pointer(run_id)
    pointer.update(attempt=1, phase="complete", pending_landing_repair=True)
    if mark != "missing":
        pointer["pending_landing_repair_attempt"] = {
            **pending_landing.marked_identity(pointer),
            "phase": "blocked" if mark == "phase-rewritten" else "complete",
        }
    runs._write_json(runs.pointer_path(run_id), pointer)
    resume.hold_delivery(run_id, "a resume deferred this delivery", head=member["head"])
    assert not pending_landing._record_path(run_id).exists()
    assert resume.delivery_hold(runs.read_pointer(run_id)) is not None

    result = pending_landing.finish_pending_landings(only_run=run_id)

    assert not runs.pointer_path(run_id).exists(), result
    assert not member["tree"].exists(), result
    row = json.loads(
        ledger.run_path(MERGED_PROJECT, run_id, cohort["repo"]).read_text()
    )
    assert row["release"]["worktree_released"] is True
    assert row["release"].get("worktree_absent") is not True
