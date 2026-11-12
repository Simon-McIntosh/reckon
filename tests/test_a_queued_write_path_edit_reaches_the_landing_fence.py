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
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims, promotion_scope, queue_admission, queue_edit, runs
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
    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher(recorded))

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
