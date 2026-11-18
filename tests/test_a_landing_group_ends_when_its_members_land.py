"""Landing journals cannot recreate promoted runs or retain integrated members."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reckon import ledger
from reckon.crew import (
    pending_landing,
    promotion_requests,
    queue_landing,
    recovery_repair_dispatch,
    runs,
)
from tests.helpers.git import git


@pytest.fixture
def group(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "seed").write_text("seed\n")
    git(repo, "add", "seed")
    git(repo, "commit", "-qm", "test: seed repository")
    base = git(repo, "rev-parse", "HEAD")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--bare", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    project = "landing-fixture"
    config = runs.crew_home().parent
    config.mkdir(parents=True, exist_ok=True)
    (config / "mounts.json").write_text(json.dumps({project: str(repo / "docs")}))
    members = {}
    for name in ("first", "second", "third"):
        tree = tmp_path / name
        git(repo, "worktree", "add", "--detach", str(tree), base)
        (tree / name).write_text(name + "\n")
        git(tree, "add", name)
        git(tree, "commit", "-qm", "test: deliver independent file")
        head = git(tree, "rev-parse", "HEAD")
        pointer = {
            "run_id": "r-" + name,
            "project": project,
            "repo": str(repo),
            "worktree": str(tree),
            "landing_group": "r-first",
            "node": {},
        }
        members[pointer["run_id"]] = {
            "pointer": pointer,
            "head": head,
            "options": {"run_id": pointer["run_id"]},
        }
    request = {
        "run_id": "r-first",
        "kind": "landing-group",
        "options": {"members": list(members.values())},
    }
    journal = {"run_id": "r-first", "request": request, "members": members}
    pending_landing.save_group(journal)
    monkeypatch.setattr(recovery_repair_dispatch, "_reviewed_run_is_busy", lambda _: "")
    monkeypatch.setattr(pending_landing, "landed_workspace_refusal", lambda *args: "")
    monkeypatch.setattr(pending_landing, "_delivered_repair", lambda *args: "")
    released = []
    from reckon.crew import promotion, promotion_release, review_landing

    def release(pointer, **kwargs):
        released.append(pointer["run_id"])
        return {"worktree_released": True}

    monkeypatch.setattr(promotion_release, "_release_run_workspace", release)
    monkeypatch.setattr(
        promotion,
        "_record_release_on_ledger",
        lambda **kwargs: (kwargs["release"], {"run_id": kwargs["run_id"]}),
    )
    monkeypatch.setattr(
        promotion, "_release_waiters_of_the_landed_section", lambda _: []
    )
    monkeypatch.setattr(review_landing, "finish_landing", lambda *args: "")
    return SimpleNamespace(
        repo=repo,
        project=project,
        members=members,
        request=request,
        journal=journal,
        released=released,
    )


def promote(group, run_id):
    member = group.members[run_id]
    path = ledger.run_path(group.project, run_id, group.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "promoted_revision": member["head"],
                "gate": "passed",
            }
        )
    )


def test_landed_members_leave_the_candidate_and_the_group_retires(group, monkeypatch):
    for run_id in ("r-first", "r-second"):
        promote(group, run_id)
        git(
            group.repo,
            "merge",
            "--no-ff",
            "-m",
            "test: integrate delivery",
            group.members[run_id]["head"],
        )
    runs._write_json(
        runs.pointer_path("r-second"), group.members["r-second"]["pointer"]
    )
    runs._write_json(runs.pointer_path("r-third"), group.members["r-third"]["pointer"])
    group.members["r-first"]["promotion"] = {"project": group.project}
    pending_landing.write_pending(
        "r-first",
        group.project,
        pointer=group.members["r-first"]["pointer"],
        head=group.members["r-first"]["head"],
    )
    group.journal["merge_repairs"] = {
        "r-first": {
            "head": group.members["r-first"]["head"],
            "main_revision": git(group.repo, "rev-parse", "HEAD"),
            "paths": ["first"],
        }
    }
    pending_landing.save_group(group.journal)
    candidates = []

    def measure(journal):
        candidates.append(list(journal["members"]))
        raise runs.CrewError("candidate held for inspection")

    monkeypatch.setattr(queue_landing, "measure_landing_group", measure)
    monkeypatch.setattr(
        promotion_requests, "_execute_command", lambda _: {"would_promote": True}
    )
    absent = []
    for _ in range(2):
        pending_landing.finish_landing_groups()
        absent.append(not runs.pointer_path("r-first").exists())
        # Model an operator discarding the stale pointer between passes.
        runs.pointer_path("r-first").unlink(missing_ok=True)
    assert absent == [True, True], (
        f"promoted pointer stayed absent on each pass: {absent}"
    )
    assert not runs.pointer_path("r-second").exists()
    assert not promotion_requests.read_promotion("r-first")
    assert candidates == [["r-third"], ["r-third"]]
    assert group.released == ["r-second"]
    promote(group, "r-third")
    git(
        group.repo,
        "merge",
        "--no-ff",
        "-m",
        "test: integrate remaining delivery",
        group.members["r-third"]["head"],
    )
    rows = pending_landing.finish_landing_groups(only_run="r-first")
    assert {row["run_id"] for row in rows} == set(group.members)
    assert {row["status"] for row in rows} == {"finished"}
    assert not pending_landing.group_path("r-first").exists()
    outcome = json.loads((runs.run_dir("r-first") / "landing-group.json").read_text())
    assert outcome["ok"] is True
    assert set(outcome["members"]) == set(group.members)
    assert outcome["candidate"] == git(group.repo, "rev-parse", "HEAD")
    assert pending_landing.finish_landing_groups() == []


def test_a_promoted_pending_record_names_an_obligation_without_a_pointer(group):
    member = group.members["r-first"]
    pending_landing.group_path("r-first").unlink()
    pointer = {**member["pointer"]}
    pointer.pop("landing_group")
    promote(group, "r-first")
    pending_landing.write_pending(
        "r-first", group.project, pointer=pointer, head=member["head"]
    )
    path = pending_landing._record_path("r-first")
    record = pending_landing._read(path)
    record["merge_conflict"] = {
        "head": member["head"],
        "main_revision": git(group.repo, "rev-parse", "HEAD"),
        "paths": ["first"],
    }
    runs._write_json(path, record)
    for _ in range(2):
        rows = pending_landing.finish_pending_landings(only_run="r-first")
        assert not runs.pointer_path("r-first").exists()
        assert len(rows) == 1
        assert rows[0]["status"] == "record-obligation"
        assert rows[0]["record"] == str(path)
        assert "promoted" in rows[0]["error"]
        assert path.exists()


def test_a_landing_mark_cannot_write_a_promoted_pointer(group):
    promote(group, "r-first")
    pointer = group.members["r-first"]["pointer"]
    with pytest.raises(runs.CrewError, match=r"promoted.*pending-landings"):
        pending_landing.mark_pending_landing_repair("r-first", pointer)
    assert not runs.pointer_path("r-first").exists()


def test_only_the_remaining_publisher_receives_candidate_evidence(group, monkeypatch):
    from reckon.crew.landing_measure import Measurement

    member = group.members["r-third"]
    member["pointer"]["suite_command"] = "true"
    group.journal["members"] = {"r-third": member}
    measured = []
    evidence = []

    def measure(pointer, run_id, head, *args, **kwargs):
        measured.append(head)
        return Measurement(command="true", head=head, suite={}, population={})

    monkeypatch.setattr(queue_landing, "_measure_delivery", measure)
    monkeypatch.setattr(queue_landing, "_measure_config", lambda _: {})
    monkeypatch.setattr(queue_landing, "_landing_code_stamp", lambda: "fixture-code")
    monkeypatch.setattr(
        queue_landing,
        "_record_suite_evidence",
        lambda run_id, receipt, **kwargs: evidence.append(run_id),
    )
    candidate = queue_landing.measure_landing_group(group.journal)
    assert measured == [candidate]
    assert evidence == ["r-third"]
    git(group.repo, "merge-base", "--is-ancestor", member["head"], candidate)
    assert not runs.pointer_path("r-first").exists()


@pytest.mark.parametrize("cause", ["live worker", "unsafe manifest"])
def test_an_unsafe_integrated_pointer_keeps_its_group(group, monkeypatch, cause):
    member = group.members["r-first"]
    git(group.repo, "merge", "--ff-only", member["head"])
    promote(group, "r-first")
    pointer = member["pointer"]
    runs._write_json(runs.pointer_path("r-first"), pointer)
    if cause == "live worker":
        monkeypatch.setattr(
            recovery_repair_dispatch, "_reviewed_run_is_busy", lambda _: cause
        )
    else:
        monkeypatch.setattr(
            pending_landing, "landed_workspace_refusal", lambda *args: cause
        )
    rows = pending_landing.finish_landing_groups()
    assert len(rows) == 1
    assert rows[0]["status"] == "group-refused"
    assert cause in rows[0]["error"]
    assert runs.read_pointer("r-first") == pointer
    assert pending_landing.group_path("r-first").exists()
    assert group.released == []


def test_retirement_records_its_outcome_before_removing_the_journal(group, monkeypatch):
    for member in group.members.values():
        git(
            group.repo,
            "merge",
            "--no-ff",
            "-m",
            "test: integrate delivery",
            member["head"],
        )
    actual = runs._write_json

    def fail_receipt(path, value):
        if path.name == "landing-group.json":
            raise OSError("outcome write unavailable")
        return actual(path, value)

    monkeypatch.setattr(runs, "_write_json", fail_receipt)
    rows = pending_landing.finish_landing_groups()
    assert rows[0]["status"] == "group-refused"
    assert "outcome write unavailable" in rows[0]["error"]
    assert pending_landing.group_path("r-first").exists()
    monkeypatch.setattr(runs, "_write_json", actual)
    rows = pending_landing.finish_landing_groups()
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"finished"}
    assert not pending_landing.group_path("r-first").exists()
    assert (runs.run_dir("r-first") / "landing-group.json").is_file()


def test_the_landing_pass_exposes_one_record_obligation(group, monkeypatch):
    from contextlib import nullcontext

    from reckon.crew import queue_admission

    pending_landing.group_path("r-first").unlink()
    member = group.members["r-first"]
    pointer = {**member["pointer"]}
    pointer.pop("landing_group")
    promote(group, "r-first")
    pending_landing.write_pending(
        "r-first", group.project, pointer=pointer, head=member["head"]
    )
    path = pending_landing._record_path("r-first")
    record = pending_landing._read(path)
    record["merge_conflict"] = {
        "head": member["head"],
        "main_revision": "unmerged",
        "paths": ["first"],
    }
    runs._write_json(path, record)
    monkeypatch.setattr(queue_admission, "plan_read_scopes", nullcontext)
    monkeypatch.setattr(
        queue_landing,
        "_land_deliveries",
        lambda **kwargs: {"landed": [], "obligations": []},
    )
    result = queue_landing.land_once()
    assert len(result["obligations"]) == 1
    assert str(path) in result["obligations"][0]["guard"]
    assert "promoted" in result["obligations"][0]["guard"]
    assert not runs.pointer_path("r-first").exists()


@pytest.mark.parametrize("conflicted", [False, True], ids=["ready", "still-conflicted"])
def test_a_delivered_promoted_repair_lands_without_rewriting_its_pointer(
    group, monkeypatch, conflicted
):
    from reckon.crew import merge

    pending_landing.group_path("r-first").unlink()
    member = group.members["r-first"]
    pointer = {**member["pointer"], "attempt": 2}
    pointer.pop("landing_group")
    promote(group, "r-first")
    pending_landing.write_pending(
        "r-first", group.project, pointer=pointer, head=member["head"]
    )
    path = pending_landing._record_path("r-first")
    record = pending_landing._read(path)
    record["merge_conflict"] = {
        "head": member["head"],
        "main_revision": "unmerged",
        "paths": ["first"],
    }
    runs._write_json(path, record)
    runs._write_json(runs.pointer_path("r-first"), pointer)
    original = runs.pointer_path("r-first").read_bytes()
    monkeypatch.setattr(
        pending_landing, "_delivered_repair", lambda *args: member["head"]
    )
    monkeypatch.setattr(pending_landing, "_repair_review_wait", lambda *args: None)
    monkeypatch.setattr(
        queue_landing, "_merge_conflicts", lambda *args: ["first"] if conflicted else []
    )
    released = []
    monkeypatch.setattr(
        merge,
        "release_landed_worktrees",
        lambda *args, **kwargs: released.append(kwargs["head"]),
    )

    def land(repository, **kwargs):
        assert runs.pointer_path("r-first").read_bytes() == original
        return {"commit": member["head"]}

    rows = pending_landing.finish_pending_landings(only_run="r-first", land=land)
    if conflicted:
        assert rows[0]["status"] == "record-obligation"
        assert rows[0]["record"] == str(path)
        assert runs.pointer_path("r-first").read_bytes() == original
        assert released == []
        return
    assert rows[0]["status"] == "finished"
    assert released == [member["head"]]
    assert not runs.pointer_path("r-first").exists()
    assert not path.exists()


def test_a_failed_release_check_keeps_the_pointer_and_group(group, monkeypatch):
    from reckon.crew import promotion

    member = group.members["r-first"]
    git(group.repo, "merge", "--ff-only", member["head"])
    promote(group, "r-first")
    pointer = member["pointer"]
    runs._write_json(runs.pointer_path("r-first"), pointer)
    monkeypatch.setattr(
        promotion,
        "_record_release_on_ledger",
        lambda **kwargs: (
            {"worktree_released": True, "landing_check": {"ok": False}},
            {"run_id": "r-first"},
        ),
    )
    rows = pending_landing.finish_landing_groups()
    assert rows[0]["status"] == "group-refused"
    assert "outcome could not be recorded" in rows[0]["error"]
    assert runs.read_pointer("r-first") == pointer
    assert pending_landing.group_path("r-first").exists()
