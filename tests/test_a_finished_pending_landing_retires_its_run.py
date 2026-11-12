"""A pending landing retires its marked run only after the remote accepts it."""

from __future__ import annotations

import json
import subprocess

from reckon import crew_run_commands
from reckon.crew import pending_landing, runs
from tests.helpers.git import git
from tests.test_a_conflicting_landing_is_repaired import (
    ORIGINAL,
    SOURCE,
    _complete_land,
    deliver_repair_gate,
)
from tests.test_a_conflicting_landing_is_repaired import (
    deliveries as deliveries,  # noqa: PLC0414 - fixture export
)


def _promoted_repair(deliver):
    first = deliver("first", "first original", "first delivered")
    repaired = deliver("repair", "first original", "second delivered")
    result = _complete_land(first)
    assert result.exit_code == 0, result.output
    result = _complete_land(repaired)
    assert result.exit_code != 0, result.output
    receipt = json.loads(result.output)
    assert receipt["steps"]["promote"]["ok"], receipt
    assert receipt["steps"]["land"]["outcome"] == "merge-conflict", receipt
    primary = git(repaired["repository"], "rev-parse", "HEAD")
    worktree = repaired["worktree"]
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
    repaired["authored_head"] = repaired["head"]
    repaired["head"] = git(worktree, "rev-parse", "HEAD")
    deliver_repair_gate(repaired)
    pointer = runs.read_pointer(repaired["run_id"])
    assert pointer["pending_landing_repair"] is True
    assert pending_landing._record_path(repaired["run_id"]).is_file()
    assert worktree.is_dir()
    return repaired


def _assert_remote_carries(run):
    assert git(run["origin"], "merge-base", run["head"], "main") == run["head"]
    assert "first delivered + second delivered" in git(
        run["origin"], "show", "main:" + SOURCE
    )


def test_a_finished_pending_landing_retires_its_run(deliveries, monkeypatch):
    deliver, _resumed = deliveries
    run = _promoted_repair(deliver)

    results = pending_landing.finish_pending_landings(only_run=run["run_id"])

    assert results[0]["status"] == "finished", results
    assert results[0]["head"] == run["head"]
    _assert_remote_carries(run)
    assert not runs.pointer_path(run["run_id"]).exists()
    assert not run["worktree"].exists()
    assert str(run["worktree"]) not in git(run["repository"], "worktree", "list")
    assert not pending_landing._record_path(run["run_id"]).exists()


def test_a_refused_push_keeps_the_run_and_its_recovery_state(deliveries, monkeypatch):
    deliver, _resumed = deliveries
    run = _promoted_repair(deliver)
    remote_before = git(run["origin"], "rev-parse", "main")
    hook = run["origin"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'push refused by fixture' >&2\nexit 1\n")
    hook.chmod(0o755)

    results = pending_landing.finish_pending_landings(only_run=run["run_id"])

    assert results[0]["status"] == "failed", results
    assert "push refused by fixture" in results[0]["error"]
    assert git(run["origin"], "rev-parse", "main") == remote_before
    assert runs.read_pointer(run["run_id"])["pending_landing_repair"] is True
    assert run["worktree"].is_dir()
    assert pending_landing._record_path(run["run_id"]).is_file()


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
    assert json.loads(result.output)["interrupted"], result.output
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
