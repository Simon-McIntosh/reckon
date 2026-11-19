"""A pending landing retires its run only after the remote accepts it.

A promotion that has not merged yet is finished by the landing pass at the head
it cited. A promotion whose merge meets a conflict is different: the run is
promoted, so its live pointer is never written back and the pending record —
which the refusal names and which carries the conflict — is the obligation a
coordinator reads.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew_run_commands
from reckon.crew import pending_landing, runs
from tests.helpers.git import git
from tests.helpers.landing_repository import PROJECT
from tests.test_a_conflicting_landing_is_repaired import (
    ORIGINAL,
    SOURCE,
    _complete_land,
    deliver_repair_gate,
)
from tests.test_a_conflicting_landing_is_repaired import (
    deliveries as deliveries,  # noqa: PLC0414 - fixture export
)


def _promoted_conflict(deliver):
    """Deliver two runs whose changes meet in a real merge conflict.

    The second delivery is promoted before its merge runs, so the conflict is
    met with a promoted row behind it. Promotion removed the live pointer, and
    the repair that would resume the delivery's worker is refused because a
    promoted run's pointer is never written back. What survives is the pending
    record, which the refusal receipt names and which carries the conflict.
    """
    first = deliver("first", "first original", "first delivered")
    result = _complete_land(first)
    assert result.exit_code == 0, result.output
    repaired = deliver("repair", "first original", "second delivered")
    result = _complete_land(repaired)
    assert result.exit_code != 0, result.output
    receipt = json.loads(Path(json.loads(result.output)["record_path"]).read_text())
    assert receipt["steps"]["promote"]["ok"], receipt
    assert receipt["steps"]["land"]["outcome"] == "merge-conflict", receipt
    repair = receipt["steps"]["land"]["repair"]
    record_path = pending_landing._record_path(repaired["run_id"])
    assert repair["status"] == "refused", repair
    assert "is promoted" in repair["reason"], repair
    assert repair["record"] == str(record_path)
    assert not runs.pointer_path(repaired["run_id"]).exists()
    assert record_path.is_file()
    record = pending_landing._read(record_path)
    assert record["merge_conflict"]["head"] == repaired["head"]
    assert record["merge_conflict"]["paths"] == [SOURCE]
    assert repaired["worktree"].is_dir()
    assert git(repaired["worktree"], "rev-parse", "HEAD") == repaired["head"]
    assert "second delivered" not in git(repaired["origin"], "show", "main:" + SOURCE)
    repaired["conflict_head"] = repaired["head"]
    return repaired


def _conflict_resolution(run):
    """Merge the primary into the delivery's worktree and gate the resolution.

    This is the repair the refusal's advice asks for, delivered into a worktree
    whose run has been promoted; the record stays the only journal naming it.
    """
    primary = git(run["repository"], "rev-parse", "HEAD")
    worktree = run["worktree"]
    conflict = subprocess.run(
        ["git", "-C", str(worktree), "merge", "--no-commit", "--no-ff", primary],
        capture_output=True,
        text=True,
        check=False,
    )
    assert conflict.returncode == 1, conflict.stdout + conflict.stderr
    assert SOURCE in conflict.stdout
    (worktree / SOURCE).write_text(
        ORIGINAL.replace("first original", "first delivered + second delivered")
    )
    git(worktree, "add", SOURCE)
    git(
        worktree,
        "commit",
        "-q",
        "-m",
        "fix: preserve both delivered results",
        "-m",
        "Combine the function changes and retain the promotion record.",
    )
    repaired_head = git(worktree, "rev-parse", "HEAD")
    run["authored_head"] = run["head"]
    run["head"] = repaired_head
    deliver_repair_gate(run)
    return run


def _assert_remote_carries(run):
    assert git(run["origin"], "merge-base", run["head"], "main") == run["head"]
    assert "first delivered + second delivered" in git(
        run["origin"], "show", "main:" + SOURCE
    )


def test_a_conflicted_promotion_owes_its_record_and_lands_nothing(deliveries):
    deliver, _resumed = deliveries
    run = _promoted_conflict(deliver)
    record_path = pending_landing._record_path(run["run_id"])
    remote_before = git(run["origin"], "rev-parse", "main")

    results = pending_landing.finish_pending_landings(only_run=run["run_id"])

    assert results[0]["status"] == "record-obligation", results
    assert results[0]["record"] == str(record_path)
    assert "is promoted" in results[0]["error"], results
    assert git(run["origin"], "rev-parse", "main") == remote_before
    assert not runs.pointer_path(run["run_id"]).exists()
    assert record_path.is_file()
    assert run["worktree"].is_dir()
    assert git(run["worktree"], "rev-parse", "HEAD") == run["head"]
    # The obligation is durable: a later pass reports the same record rather
    # than retrying a merge nothing can finish.
    again = pending_landing.finish_pending_landings(only_run=run["run_id"])
    assert again[0]["status"] == "record-obligation", again
    assert again[0]["record"] == str(record_path)
    assert not runs.pointer_path(run["run_id"]).exists()


def test_a_delivered_repair_keeps_the_record_and_the_worktree(deliveries):
    deliver, _resumed = deliveries
    run = _promoted_conflict(deliver)
    _conflict_resolution(run)
    record_path = pending_landing._record_path(run["run_id"])
    record = pending_landing._read(record_path)
    assert record["merge_conflict"]["head"] == run["conflict_head"]
    remote_before = git(run["origin"], "rev-parse", "main")

    results = pending_landing.finish_pending_landings(only_run=run["run_id"])

    assert results[0]["status"] == "record-obligation", results
    assert results[0]["record"] == str(record_path)
    assert "is promoted" in results[0]["error"], results
    assert not runs.pointer_path(run["run_id"]).exists()
    assert pending_landing._record_path(run["run_id"]).is_file()
    assert run["worktree"].is_dir()
    assert git(run["worktree"], "rev-parse", "HEAD") == run["head"]
    assert git(run["origin"], "rev-parse", "main") == remote_before


def test_an_already_removed_pointer_does_not_prevent_finishing(deliveries, monkeypatch):
    deliver, _resumed = deliveries
    run = deliver("interrupted", "first original", "first delivered + second delivered")
    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            crew_run_commands,
            "_land_a_promoted_run",
            lambda *a, **k: ({"ok": False, "interrupted": True}, 1),
        )
        result = _complete_land(run)
    assert result.exit_code != 0, result.output
    receipt = json.loads(Path(json.loads(result.output)["record_path"]).read_text())
    assert receipt["interrupted"], receipt
    assert (
        pending_landing._promoted_row("proj", run["run_id"])["promoted_revision"]
        == run["head"]
    )
    assert not runs.pointer_path(run["run_id"]).exists()
    assert pending_landing._record_path(run["run_id"]).is_file()

    results = pending_landing.finish_pending_landings(only_run=run["run_id"])

    assert results[0]["status"] == "finished", results
    _assert_remote_carries(run)
    assert not runs.pointer_path(run["run_id"]).exists()
    assert not pending_landing._record_path(run["run_id"]).exists()


def test_a_refused_push_keeps_the_run_and_its_recovery_state(deliveries, monkeypatch):
    """A push the remote refuses leaves the run, its record and its worktree.

    An interrupted promotion's recovery state is the pending record and the
    worktree its reviewed head was authored in. A refused push is the failure
    that leaves the merge itself sound, so nothing may be retired on the way
    out: the refusal is written onto the record as the attempt's own error, the
    record stays for the next pass to retry, and the worktree is left where the
    worker can reach it.
    """
    deliver, _resumed = deliveries
    run = deliver("refused", "first original", "first delivered + second delivered")
    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            crew_run_commands,
            "_land_a_promoted_run",
            lambda *a, **k: ({"ok": False, "interrupted": True}, 1),
        )
        result = _complete_land(run)
    assert result.exit_code != 0, result.output
    record_path = pending_landing._record_path(run["run_id"])
    assert record_path.is_file()
    assert not runs.pointer_path(run["run_id"]).exists()
    remote_before = git(run["origin"], "rev-parse", "main")
    hook = run["origin"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'push refused by fixture' >&2\nexit 1\n")
    hook.chmod(0o755)

    results = pending_landing.finish_pending_landings(only_run=run["run_id"])

    assert results[0]["status"] == "failed", results
    assert "push refused by fixture" in results[0]["error"], results
    assert git(run["origin"], "rev-parse", "main") == remote_before
    assert record_path.is_file()
    record = pending_landing._read(record_path)
    assert record["attempts"] == 1
    assert "push refused by fixture" in record["last_error"]
    assert not runs.pointer_path(run["run_id"]).exists()
    assert run["worktree"].is_dir()
    assert git(run["worktree"], "rev-parse", "HEAD") == run["head"]


def _unfinished_promotion(deliver, name):
    """Deliver a run whose promotion stopped between its record and its row."""
    run = deliver(name, "first original", "first delivered")
    pointer = runs.read_pointer(run["run_id"])
    runs._write_json(
        runs.pointer_path(run["run_id"]),
        {
            **pointer,
            "admission_landing": {
                "head": run["head"],
                "guard": "crew land",
                "status": "attempting",
                "attempts": 0,
                "failed_at": 0.0,
            },
        },
    )
    pending_landing.write_pending(run["run_id"], PROJECT, now=1000.0 - 10 * 60)
    return run


def test_an_interrupted_promotion_keeps_its_record_until_its_attempt_is_retired(
    deliveries, monkeypatch
):
    """A pass that ends before it clears the record leaves the retry reachable.

    The unfinished-promotion branch clears the pending-landing record and
    retires the interrupted land attempt. A pass killed between the two would,
    if it cleared first, leave a live pointer carrying an ``attempting``
    attempt and no record — a hold no later sweep can lift, because the record
    is the evidence the attempt's promotion stopped. So the attempt and its
    pointer are retired before the record goes.
    """
    deliver, _resumed = deliveries
    run = _unfinished_promotion(deliver, "interrupted-promotion")
    real_clear = pending_landing.clear_pending

    def clear_dies(run_id):
        raise RuntimeError("the pass ended before it could clear its record")

    monkeypatch.setattr(pending_landing, "clear_pending", clear_dies)
    with pytest.raises(RuntimeError, match="ended before"):
        pending_landing.finish_pending_landings(now=1000.0, only_run=run["run_id"])

    # The pass died at the record's removal, so what reached disk is the whole
    # of its work: the attempt is retired and the record is still there.
    assert pending_landing._record_path(run["run_id"]).is_file()
    assert (
        runs.read_pointer(run["run_id"])["admission_landing"]["status"] == "interrupted"
    )

    monkeypatch.setattr(pending_landing, "clear_pending", real_clear)
    results = pending_landing.finish_pending_landings(
        now=1000.0, only_run=run["run_id"]
    )

    assert results == [
        {
            "run_id": run["run_id"],
            "status": "retried-unfinished",
            "attempt_retired": True,
        }
    ], results
    assert not pending_landing._record_path(run["run_id"]).exists()
    assert (
        runs.read_pointer(run["run_id"])["admission_landing"]["status"] == "interrupted"
    )


def test_a_record_survives_when_the_attempt_cannot_be_retired(deliveries, monkeypatch):
    """A failed retirement keeps the record, which is the next sweep's evidence.

    The record is cleared only once the attempt is retired. If the pointer
    cannot be written, the attempt stays unanswered and the record is the only
    thing that tells a later sweep why; clearing it would leave the hold this
    branch exists to lift.
    """
    deliver, _resumed = deliveries
    run = _unfinished_promotion(deliver, "unretired-promotion")
    real_retire = pending_landing._retire_interrupted_landing
    monkeypatch.setattr(
        pending_landing, "_retire_interrupted_landing", lambda run_id: False
    )

    results = pending_landing.finish_pending_landings(
        now=1000.0, only_run=run["run_id"]
    )

    assert results == [
        {
            "run_id": run["run_id"],
            "status": "attempt-not-retired",
            "attempt_retired": False,
        }
    ], results
    assert pending_landing._record_path(run["run_id"]).is_file()
    assert (
        runs.read_pointer(run["run_id"])["admission_landing"]["status"] == "attempting"
    )

    monkeypatch.setattr(pending_landing, "_retire_interrupted_landing", real_retire)
    results = pending_landing.finish_pending_landings(
        now=1000.0, only_run=run["run_id"]
    )

    assert results == [
        {
            "run_id": run["run_id"],
            "status": "retried-unfinished",
            "attempt_retired": True,
        }
    ], results
    assert not pending_landing._record_path(run["run_id"]).exists()
    assert (
        runs.read_pointer(run["run_id"])["admission_landing"]["status"] == "interrupted"
    )


def test_a_later_attempt_is_never_written_back(deliveries):
    """A promoted run's record is the obligation; no attempt is given a pointer.

    The pass reads, but never rewrites, a pointer that a later attempt left: the
    record names the conflict and the run is reported as an obligation, with its
    worktree, its pointer and the branch it could not land on all left as they
    were.
    """
    deliver, _resumed = deliveries
    run = _promoted_conflict(deliver)
    record_path = pending_landing._record_path(run["run_id"])
    captured = pending_landing._read(record_path)["pointer"]
    captured_attempt = int(captured.get("attempt") or 0)
    newer = {**captured, "attempt": captured_attempt + 1, "phase": "running"}
    runs._write_json(runs.pointer_path(run["run_id"]), newer)
    written = runs.pointer_path(run["run_id"]).read_bytes()

    results = pending_landing.finish_pending_landings(only_run=run["run_id"])

    assert results[0]["status"] == "record-obligation", results
    assert results[0]["record"] == str(record_path)
    assert runs.pointer_path(run["run_id"]).read_bytes() == written
    assert runs.read_pointer(run["run_id"]) == newer
    assert int(runs.read_pointer(run["run_id"])["attempt"]) == captured_attempt + 1
    assert run["worktree"].is_dir()
    assert git(run["worktree"], "rev-parse", "HEAD") == run["conflict_head"]
    assert pending_landing._record_path(run["run_id"]).is_file()
