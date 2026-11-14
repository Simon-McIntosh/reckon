"""A write path added to a queued request reaches the launched run's fence.

A request that has not launched is still the coordinator's to change: ``crew
queue edit --write-path`` widens its declared scope while it waits. The edit is
only worth anything if the launched run's declared scope and its registered
claims are the paths the edit named, because that is what the landing scope
check reads and what a peer's claim is reconciled against.
"""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import (
    dispatch_claims,
    promotion_scope,
    queue_admission,
    queue_edit,
    runs,
)
from reckon.crew.node import TaskNode

PROJECT = "proj"
SESSION = "coordinator-a"
RUN_ID = "r-queued-write-path"
ORIGINAL = "src/queue.py"
ADDED = "src/added.py"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def crew_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    from reckon import crew_dispatch_commands

    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: dict(CONFIG)
    )

    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    quote = '"'
    plan.write_text(
        "\n".join(
            [
                "<!doctype html>",
                f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>",
                f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>",
                f"<meta name={quote}plan-slug{quote} content={quote}plan-a{quote}>",
                f"<h2 id={quote}dispatch{quote}>Dispatch</h2>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repository / ORIGINAL).parent.mkdir(parents=True, exist_ok=True)
    (repository / ORIGINAL).write_text("original\n", encoding="utf-8")
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs", "skills", ORIGINAL)
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _queue(repository: Path) -> dict:
    node = TaskNode(
        id="queued-write-path",
        goal="edit a queued request's write scope",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports the widened path inside the launched fence",
        manifest_path=str(runs.run_dir(RUN_ID) / "manifest.md"),
        write_paths=[ORIGINAL],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": "clive",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": SESSION,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


def _config_for():
    config = json.loads(json.dumps(CONFIG))
    return config, lambda _pointer: config


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return 1

    return launch


def _launched_record() -> dict:
    record = runs.read_pointer(RUN_ID)
    # Promotion reads the run through the same merge of its pointer and its
    # dispatch-time detail file.
    return {**record, **runs.read_dispatch_detail(record)}


def _commit_the_added_path(record: dict) -> list[str]:
    """Stand in for the worker: change the added path and cite the commit."""
    worktree = Path(str(record["worktree"]))
    added = worktree / ADDED
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("written by the stub worker\n", encoding="utf-8")
    _git(worktree, "add", "--", ADDED)
    _git(worktree, "commit", "-q", "-m", "feat: write the added path")
    base = str(record.get("base_sha") or record.get("base") or "HEAD~1")
    changed = _git(worktree, "diff", "--name-only", f"{base}..HEAD")
    return [line for line in changed.splitlines() if line.strip()]


def test_a_queued_write_path_edit_is_the_launched_runs_fence(
    crew_repo: Path,
) -> None:
    runs.queue_dispatch(_queue(crew_repo))
    queue_edit.edit(RUN_ID, write_paths=[ORIGINAL, ADDED], project=PROJECT)

    _, resolve = _config_for()
    recorded: list = []
    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert result["launched"] == [RUN_ID]
    record = _launched_record()
    declared = list((record.get("node") or {}).get("write_paths") or ())
    assert ADDED in declared, declared

    # The changed path the stub worker committed is inside the fence the
    # landing scope check reads, so no path is named as outside it.
    changed = _commit_the_added_path(record)
    assert ADDED in changed, changed
    outside = promotion_scope._outside_declared_scope(
        changed,
        declared,
        record=record,
        tree=Path(str(record["worktree"])),
    )
    assert outside == ()

    # The run's registered claims are its declared grants, so the added path is
    # held as a claim exactly as the originally dispatched ones are.
    granted = [grant["path"] for grant in dispatch_claims.declared_write_grants(record)]
    assert ADDED in granted, granted


SECTION_PATHS = (
    "reckon/crew/promotion_gate.py",
    "tests/test_a_gate_replay_runs_on_the_measure_partition.py",
)
HELD_RUN = "r-held-write-path"


def _section_plan(repository: Path) -> None:
    for path in SECTION_PATHS:
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("seed\n", encoding="utf-8")
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.write_text(
        '<!doctype html><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan"><meta name="plan-slug" content="plan-a">'
        '<h2 id="s1">S</h2><section data-reckon="section" data-id="s1" '
        'data-effort-hours="1.0" data-capability-version="1.0" '
        'data-capability-class="general" data-capability-reasoning="standard" '
        'data-capability-verification="strict" data-capability-risk="low" '
        'data-status="implementable"></section>'
        "<p><strong>Write paths:</strong> "
        + " and ".join(f"<code>{path}</code>" for path in SECTION_PATHS)
        + ".</p>",
        encoding="utf-8",
    )


def _queue_held(repository: Path, run_id: str = HELD_RUN) -> None:
    node = TaskNode(
        id="held-write-path",
        goal="hold a request until its plan review lands",
        plan="plan-a",
        section="s1",
        done_when="pytest reports the held request released with its edit intact",
        manifest_path=str(runs.run_dir(run_id) / "manifest.md"),
        negative_control="restore the snapshot-derived release so the edit is dropped",
        write_paths=list(SECTION_PATHS),
        time_budget="20m",
        spec_level="exact",
    ).as_dict()
    node["section_derived"] = {"write_paths": "stale-digest"}
    joined = datetime.now(UTC).isoformat()
    runs.queue_dispatch(
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "node": node,
            "backend": "clive",
            "launch": "cli",
            "local": True,
            "phase": "queued",
            "queued_at": joined,
            "created_at": joined,
            "dispatch_options": {
                "project": PROJECT,
                "plan_slug": "plan-a",
                "section": "s1",
                "session": SESSION,
                "local": True,
                "no_watch": True,
                "overrides": [],
            },
        }
    )

    def hold(current: dict) -> dict:
        current["phase"] = "held"
        current["queue_hold"] = {
            "automatic": True,
            "reason": "plan-review-owed",
            "prior_phase": "queued",
            "started_at": joined,
        }
        return current

    runs._mutate_pointer(run_id, hold)


def test_a_release_keeps_the_write_path_a_coordinator_edited(
    crew_repo: Path,
) -> None:
    _section_plan(crew_repo)
    _queue_held(crew_repo)

    # The admission pass snapshots the live pointers at the start of its pass,
    # before the coordinator's edit reaches the queue.
    live = [deepcopy(runs.read_pointer(HELD_RUN))]
    queue_edit.edit(HELD_RUN, write_paths=[*SECTION_PATHS, ADDED], project=PROJECT)
    edited = runs.read_pointer(HELD_RUN)["node"]
    assert ADDED in edited["write_paths"]
    assert "write_paths" not in (edited.get("section_derived") or {})

    released = queue_admission._release_plan_review_holds(
        live, config_for=lambda _pointer: {}
    )

    assert released == [HELD_RUN]
    written = runs.read_pointer(HELD_RUN)
    assert written["phase"] == "queued"
    assert "queue_hold" not in written
    # The coordinator's path survives the release and stays the coordinator's:
    # the field is not re-derived from the section and is not re-marked.
    assert set(written["node"]["write_paths"]) == {*SECTION_PATHS, ADDED}, written[
        "node"
    ]["write_paths"]
    assert "write_paths" not in (written["node"].get("section_derived") or {})
    # The pass launches from its own in-memory records, so the record it hands
    # the launcher is the one the release wrote, not the one it snapshotted.
    assert ADDED in live[0]["node"]["write_paths"]


def test_a_release_still_derives_the_fields_the_section_owns(
    crew_repo: Path,
) -> None:
    unedited = "r-held-unedited"
    _section_plan(crew_repo)
    _queue_held(crew_repo, unedited)

    live = [deepcopy(runs.read_pointer(unedited))]
    released = queue_admission._release_plan_review_holds(
        live, config_for=lambda _pointer: {}
    )

    assert released == [unedited]
    written = runs.read_pointer(unedited)
    assert written["phase"] == "queued"
    # A field the section still supplies is re-derived and stays marked, so a
    # later release brings it to the text the plan moved to again.
    assert list(written["node"]["write_paths"]) == list(SECTION_PATHS)
    assert "write_paths" in (written["node"].get("section_derived") or {})
