"""A promoted row ends a live pointer without losing pending landing work."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reckon import ledger
from reckon.crew import pending_landing, plan_review, queue_landing, runs, station
from tests.helpers.git import git

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
        run_id = f"r-{name}"
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
    beat.on_report = lambda project, session, report: reports.append(report)
    assert beat.request(PROJECT, "owner", moment=datetime.now(UTC), changed=True)
    assert len(reports) == 1
    return reports[0]


def test_promoted_pointers_retire_and_never_return(cohort):
    repo, _head, pointers = cohort
    identity_fields = ("run_id", "attempt", "phase", "landing_group")
    unpromoted = runs.read_pointer("r-unpromoted")
    expected_identity = {key: unpromoted[key] for key in identity_fields}
    group = pending_landing._read(pending_landing.group_path("r-grouped"))
    position = list(group["members"]).index("r-unpromoted")
    unreadable = runs.pointer_path("r-unreadable").read_bytes()
    assert pending_landing._promoted_row(PROJECT, "r-orphan") is not None
    assert pending_landing._promoted_row(PROJECT, "r-unreadable") is None
    assert not pending_landing._promoted_row_absent(PROJECT, "r-unreadable")
    assert not pending_landing._record_path("r-orphan").exists()
    for _ in range(2):
        queue_landing.land_once()
        _station_sweep()
        for name in ("orphan", "journal", "grouped"):
            assert not runs.pointer_path(pointers[name]["run_id"]).exists(), name
        assert runs.pointer_path("r-unpromoted").exists()
        current = runs.read_pointer("r-unpromoted")
        assert {key: current[key] for key in identity_fields} == expected_identity
        assert runs.pointer_path("r-unreadable").read_bytes() == unreadable
        group = pending_landing._read(pending_landing.group_path("r-grouped"))
        assert list(group["members"]) == ["r-unpromoted"]
        assert list(group["members"]).index("r-unpromoted") == position
        assert group["members"]["r-unpromoted"]["pointer"] == pointers["unpromoted"]
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
