"""A conflict repair is withheld from a run already landed or being landed.

A landing that meets a conflict resumes the delivery's worker to resolve it.
Two later states must not start that resume: the conflicting head is already an
ancestor of the primary branch, because a coordinator resolved the conflict and
landed it by hand; or a coordinator is landing the run right now, in which case
the resume races that landing. And a repair resume that does start must not
rewrite the manifest's status word before its worker writes one — a manifest
flipped to in-progress is the run the supervisor later cannot retire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import pending_landing, resumption, runs
from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair
from tests.helpers.git import git
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout

SOURCE = "delivered.py"
ORIGINAL = "def answer():\n    return 1\n"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A real origin and checkout, a conflict writer, and a resume spy."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    (repository / SOURCE).write_text(ORIGINAL, encoding="utf-8")
    git(repository, "add", SOURCE)
    git(repository, "commit", "-q", "-m", "test: define the delivered function")
    git(repository, "push", "-q", "origin", "main")
    base = git(repository, "rev-parse", "HEAD")
    resumed = []
    real_resume = resumption._resume

    def resume(run_id, record, **kwargs):
        resumed.append({"run_id": run_id, "record": record, "kwargs": kwargs})
        return {"turn": 1}

    monkeypatch.setattr(resumption, "_resume", resume)

    def deliver(name):
        """A delivered run on its own worktree, with the conflict recorded."""
        run_id = "r-repair-" + name
        worktree = tmp_path / name
        git(repository, "worktree", "add", "--detach", str(worktree), base)
        (worktree / SOURCE).write_text(
            ORIGINAL.replace("return 1", "return 2"), encoding="utf-8"
        )
        git(worktree, "add", SOURCE)
        git(worktree, "commit", "-q", "-m", "test: change the delivered function")
        head = git(worktree, "rev-parse", "HEAD")
        manifest = tmp_path / (name + "-manifest.md")
        manifest.write_text(
            "node: " + name + "\nstatus: complete\ncommits: " + head + "\n"
        )
        conflict = {
            "outcome": "merge-conflict",
            "paths": [SOURCE],
            "main_revision": base,
            "head": head,
            "round_id": f"{run_id}:merge:{base}@{head}",
            "status": "held",
        }
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "manifest_path": str(manifest),
            "base_sha": base,
            "launch": "cli",
            "session_id": "session-" + name,
            "role": "implement",
            "member": name,
            "backend": "native",
            "created_at": "2026-01-01T00:00:00Z",
            "merge_conflict": dict(conflict),
            "pending_landing": True,
            "node": {
                "id": name,
                "plan": "plan-a",
                "section": "section",
                "role": "implement",
                "time_budget": "30m",
                "write_paths": [SOURCE],
            },
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        pending_landing.write_pending(run_id, PROJECT, pointer=pointer, head=head)
        record_path = pending_landing._record_path(run_id)
        pending = json.loads(record_path.read_text(encoding="utf-8"))
        pending["merge_conflict"] = dict(conflict)
        record_path.write_text(json.dumps(pending), encoding="utf-8")
        return {
            "run_id": run_id,
            "head": head,
            "worktree": str(worktree),
            "pointer": pointer,
        }

    return {
        "origin": origin,
        "repository": repository,
        "resumed": resumed,
        "deliver": deliver,
        "base": base,
        "real_resume": real_resume,
        "tmp_path": tmp_path,
    }


def land_the_head(world, run):
    """Resolve the conflict as a coordinator does: merge the head into primary."""
    repository = world["repository"]
    git(
        repository,
        "merge",
        "--no-ff",
        "-m",
        "merge: land the repaired head",
        run["head"],
    )
    return git(repository, "rev-parse", "HEAD")


def lose_the_remote(world):
    """Make the remote unreadable, as a flaky or absent origin is."""
    git(
        world["repository"],
        "remote",
        "set-url",
        "origin",
        str(world["tmp_path"] / "no-such-origin.git"),
    )


def pending_conflict(run_id):
    path = pending_landing._record_path(run_id)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("merge_conflict") or {}


def test_a_landed_head_gets_no_repair_and_its_conflict_is_cleared(world):
    """The coordinator already landed the head: no resume, no stale conflict."""
    run = world["deliver"]("landed")
    primary = land_the_head(world, run)
    # The local primary branch holds the head; the remote cannot be asked, so
    # only this checkout's own branch can say the landing already happened.
    lose_the_remote(world)
    receipt = dispatch_merge_repair(
        run["pointer"],
        head=run["head"],
        primary=primary,
        paths=[SOURCE],
        promoted=True,
    )
    assert world["resumed"] == []
    assert receipt["status"] == "refused", receipt
    assert "landed" in json.dumps(receipt), receipt
    assert pending_conflict(run["run_id"]) == {}
    pointer = runs.read_pointer(run["run_id"]) or {}
    assert not pointer.get("merge_conflict"), pointer


def test_a_coordinator_landing_in_progress_withholds_the_repair(world):
    """A recorded land attempt holds the repair until that landing ends."""
    run = world["deliver"]("landing")
    pointer = runs.read_pointer(run["run_id"])
    pointer["admission_landing"] = {"head": run["head"], "status": "attempting"}
    runs._write_json(runs.pointer_path(run["run_id"]), pointer)
    receipt = dispatch_merge_repair(
        run["pointer"],
        head=run["head"],
        primary=world["base"],
        paths=[SOURCE],
        promoted=True,
    )
    assert world["resumed"] == []
    assert receipt["status"] == "held", receipt
    assert pending_conflict(run["run_id"]) != {}, (
        "the conflict is kept for the next pass"
    )


def _stub_the_launch(module, monkeypatch):
    """Replace everything the launch does around the manifest it reads."""
    from types import SimpleNamespace

    from reckon.crew import dispatch_launch

    monkeypatch.setattr(module, "resume_plan", lambda *a, **k: "plan")
    monkeypatch.setattr(
        module, "_with_revision_suite_block", lambda advice, record: advice
    )
    monkeypatch.setattr(module, "_write_resume_prompt", lambda *a, **k: None)
    monkeypatch.setattr(module, "_require_fleet_gate_open", dict)
    monkeypatch.setattr(module, "mark_resume_launching", lambda run_id: None)
    monkeypatch.setattr(module, "record_resumption", lambda *a, **k: {})
    monkeypatch.setattr(
        dispatch_launch,
        "reserve_launch_attempt",
        lambda *a, **k: SimpleNamespace(attempt=1),
    )
    monkeypatch.setattr(
        dispatch_launch, "release_launch_reservation", lambda *a, **k: None
    )


def test_a_live_conflict_is_resumed_and_the_status_is_left_alone(world, monkeypatch):
    """Neither condition holds: resume, and leave the manifest status as found."""
    run = world["deliver"]("live")
    launched = []

    def spawn(plan, **kwargs):
        launched.append(plan)
        return 4_194_302

    monkeypatch.setattr(resumption, "_resume", world["real_resume"])
    monkeypatch.setattr(resumption, "_spawn", spawn)
    _stub_the_launch(resumption, monkeypatch)
    manifest = Path(run["pointer"]["manifest_path"])
    receipt = dispatch_merge_repair(
        run["pointer"],
        head=run["head"],
        primary=world["base"],
        paths=[SOURCE],
        promoted=True,
    )
    assert receipt["status"] == "resumed", receipt.get("reason") + repr(receipt)
    assert len(launched) == 1
    text = manifest.read_text(encoding="utf-8")
    assert text.startswith("node: live\nstatus: complete\n"), text
