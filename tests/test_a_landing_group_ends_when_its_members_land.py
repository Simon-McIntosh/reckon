"""Landing journals cannot recreate promoted runs or retain integrated members."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reckon import crew_run_commands as commands
from reckon import ledger
from reckon.crew import (
    merge,
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
    monkeypatch.setattr(
        recovery_repair_dispatch, "_reviewed_run_is_busy", lambda _: "busy"
    )
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
    for _ in range(2):
        pending_landing.finish_landing_groups()
        assert not runs.pointer_path("r-first").exists(), (
            "a promoted member's pointer was recreated"
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
    rows = pending_landing.finish_landing_groups()
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
    with pytest.raises(runs.CrewError, match="promoted.*pending-landings"):
        pending_landing.mark_pending_landing_repair("r-first", pointer)
    assert not runs.pointer_path("r-first").exists()
