"""A promoted row ends a live pointer without losing pending landing work."""

# ruff: noqa: F811 - imported pytest fixtures are named by test parameters

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reckon import _store, ledger
from reckon.crew import pending_landing, plan_review, queue_landing, runs, station
from tests.helpers.git import git
from tests.helpers.landing_repository import PLAN
from tests.test_a_merged_promoted_run_is_released import (
    cohort as workspace_cohort,  # noqa: F401 - shared workspace and waiter fixture
)

PROJECT = "proj"


@pytest.fixture()
def cohort(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("delivered\n")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-qm", "test: seed delivery")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    unlanded_tree = tmp_path / "unlanded"
    git(repo, "worktree", "add", "--detach", str(unlanded_tree), head)
    (unlanded_tree / "pending.txt").write_text("not on main\n")
    git(unlanded_tree, "add", "pending.txt")
    git(unlanded_tree, "commit", "-qm", "test: deliver pending member")
    unlanded_head = git(unlanded_tree, "rev-parse", "HEAD")
    pointers = {}
    for name in ("orphan", "journal", "grouped", "unpromoted", "unreadable"):
        run_id = "r-awaiting" if name == "unpromoted" else f"r-{name}"
        directory = runs.run_dir(run_id)
        directory.mkdir(parents=True)
        manifest = directory / "manifest.md"
        manifest.write_text(f'status: complete\ncommits: ["{head}"]\nblockers: none\n')
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": head,
            "phase": "complete",
            "attempt": 1,
            "session": "owner",
            "role": "implement",
            "launch": "in-harness",
            "manifest_path": str(manifest),
            "node": {
                "id": name,
                "role": "implement",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        }
        if name == "unreadable":
            pointer["phase"] = "ended"
        if name in {"orphan", "unreadable"}:
            pointer.pop("worktree")
        if name in {"journal", "grouped"}:
            tree = tmp_path / name
            git(repo, "worktree", "add", "--detach", str(tree), head)
            pointer["worktree"] = str(tree)
        if name == "unpromoted":
            pointer["worktree"] = str(unlanded_tree)
            manifest.write_text(
                f'status: complete\ncommits: ["{unlanded_head}"]\nblockers: none\n'
            )
        if name in {"orphan", "journal", "unreadable"}:
            pointer.update(
                pending_landing_repair=True,
                pending_landing_repair_attempt=pending_landing.pointer_attempt(pointer),
            )
        if name in {"grouped", "unpromoted"}:
            pointer["landing_group"] = "r-grouped"
        runs._write_json(runs.pointer_path(run_id), pointer)
        if name != "unpromoted":
            ledger.write_run_file(
                PROJECT,
                {
                    **pointer,
                    "promoted_revision": head,
                    "gate": "passed",
                    "commits": [head],
                },
                repo,
            )
        pointers[name] = pointer
    ledger.run_path(PROJECT, "r-unreadable", repo).write_text("{unreadable")
    pending_landing.write_pending(
        "r-journal", PROJECT, pointer=pointers["journal"], head=head
    )
    members = {
        pointers[name]["run_id"]: {
            "pointer": pointers[name],
            "head": unlanded_head if name == "unpromoted" else head,
            "options": {},
        }
        for name in ("unpromoted", "grouped")
    }
    pending_landing.save_group(
        {
            "run_id": "r-grouped",
            "members": members,
            "request": {
                "run_id": "r-grouped",
                "options": {"members": list(members.values())},
            },
        }
    )
    # Plan reviews have their own dispatch tests; this cohort has no plan.
    monkeypatch.setattr(
        plan_review, "compose_owed_plan_reviews", lambda *args, **kwargs: {}
    )
    return repo, head, pointers


def _station_sweep():
    reports = []
    beat = station.SweepBeat(start=lambda run: run())
    # The station's report hook is handed the pass's memo counts beside the
    # report; this cohort cares about the report alone.
    beat.on_report = lambda project, session, report, _counts: reports.append(report)
    assert beat.request(PROJECT, "owner", moment=datetime.now(UTC), changed=True)
    assert len(reports) == 1
    return reports[0]


def test_promoted_pointers_retire_and_never_return(cohort):
    repo, _head, pointers = cohort
    identity_fields = ("run_id", "attempt", "phase", "landing_group")
    unpromoted = runs.read_pointer("r-awaiting")
    expected_identity = {key: unpromoted[key] for key in identity_fields}
    group = pending_landing._read(pending_landing.group_path("r-grouped"))
    position = list(group["members"]).index("r-awaiting")
    unreadable = runs.read_pointer("r-unreadable")
    unreadable_identity = {key: unreadable.get(key) for key in identity_fields}
    assert pending_landing._promoted_row(PROJECT, "r-orphan") is not None
    assert pending_landing._promoted_row(PROJECT, "r-unreadable") is None
    assert not pending_landing._promoted_row_absent(PROJECT, "r-unreadable")
    assert not pending_landing._record_path("r-orphan").exists()
    for _ in range(2):
        queue_landing.land_once()
        _station_sweep()
        for name in ("orphan", "journal", "grouped"):
            assert not runs.pointer_path(pointers[name]["run_id"]).exists(), name
        assert runs.pointer_path("r-awaiting").exists()
        current = runs.read_pointer("r-awaiting")
        assert {key: current[key] for key in identity_fields} == expected_identity
        current_unreadable = runs.read_pointer("r-unreadable")
        assert {
            key: current_unreadable.get(key) for key in identity_fields
        } == unreadable_identity
        group = pending_landing._read(pending_landing.group_path("r-grouped"))
        assert list(group["members"]) == ["r-awaiting"]
        assert list(group["members"]).index("r-awaiting") == position
        assert group["members"]["r-awaiting"]["pointer"] == pointers["unpromoted"]
    assert (repo / "seed.txt").read_text() == "delivered\n"


def test_station_sweep_retires_before_any_pointer_rewrite(cohort, monkeypatch):
    _repo, _head, pointers = cohort
    writes = []
    original = runs._write_json

    def trace(path, payload):
        if path == runs.pointer_path("r-orphan"):
            import traceback

            writes.append("".join(traceback.format_stack()))
        return original(path, payload)

    monkeypatch.setattr(runs, "_write_json", trace)
    _station_sweep()
    assert not writes, "\n".join(writes)
    assert not runs.pointer_path(pointers["orphan"]["run_id"]).exists()


def test_retiring_an_orphan_never_creates_a_landing_journal(cohort):
    _repo, _head, pointers = cohort
    pointer = runs.read_pointer(pointers["orphan"]["run_id"])
    path = pending_landing._record_path(pointer["run_id"])
    assert not path.exists()

    assert (
        pending_landing.retire_promoted_pointer(pointer)
        == pending_landing.POINTER_RETIRED
    )

    assert not runs.pointer_path(pointer["run_id"]).exists()
    assert not path.exists(), "retirement must not revive a finished landing"


def test_station_releases_group_workspace_receipt_and_section_waiter(workspace_cohort):
    repo = workspace_cohort["repo"]
    clean = workspace_cohort["members"]["clean"]
    waiting = workspace_cohort["members"]["unmerged"]
    pointer = {**clean["pointer"], "landing_group": clean["pointer"]["run_id"]}
    run_id = pointer["run_id"]
    runs._write_json(runs.pointer_path(run_id), pointer)
    waiting_pointer = {**waiting["pointer"], "landing_group": run_id}
    runs._write_json(runs.pointer_path(waiting_pointer["run_id"]), waiting_pointer)
    members = {
        run_id: {"pointer": pointer, "head": clean["head"], "options": {}},
        waiting_pointer["run_id"]: {
            "pointer": waiting_pointer,
            "head": waiting["head"],
            "options": {},
        },
    }
    pending_landing.save_group(
        {
            "run_id": run_id,
            "members": members,
            "request": {
                "run_id": run_id,
                "options": {"members": list(members.values())},
            },
        }
    )
    assert clean["tree"].exists()
    assert runs.read_pointer("r-waiter")["phase"] == "awaiting-upstream"
    beat = station.SweepBeat(sweep=lambda *args, **kwargs: {}, start=lambda run: run())

    assert beat.request(PROJECT, "owner", moment=datetime.now(UTC), changed=True)

    assert not runs.pointer_path(run_id).exists()
    assert not clean["tree"].exists(), "group retirement must release the workspace"
    row = json.loads(ledger.run_path(PROJECT, run_id, repo).read_text())
    assert row["release"]["worktree_released"] is True
    state, _version = _store.read_plan(PROJECT, PLAN, repo)
    assert state["section_declarations"]["s1"] == "done"
    assert runs.read_pointer("r-waiter")["phase"] == "queued"
    group = pending_landing._read(pending_landing.group_path(run_id))
    assert list(group["members"]) == [waiting_pointer["run_id"]]
    assert runs.pointer_path(waiting_pointer["run_id"]).exists()
    assert waiting["tree"].exists()
