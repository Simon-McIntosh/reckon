"""A dispatch classifies only the runs of the session that asked for it.

The dispatch backlog check asks which of a session's own finished runs are
unfinished business. Answering that needs one classification per candidate run,
and each classification reads that run's manifest, stream and stored review.
The base revision classified every live pointer on the fleet, whichever session
owned it, so one dispatch paid a cost that grew with every other session's
queue. The fence is about this session's own duties, so a pointer recorded as
another session's is skipped before it is classified: the check costs the same
at two hundred peer pointers as at none.

The tests drive the fence the way dispatch and the queue writer drive it, and
count classifications at the classifier itself. One control holds that count to
its meaning: the project-wide call, which is what the drain asks for, still
classifies every pointer the fleet holds, so a zero from the session-scoped
call is the scope filter and not an inert instrument.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, recovery_watch, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
SESSION = "coordinator-a"
PEER_SESSION = "coordinator-b"
GRACE = "5m"
PEER_POINTERS = 200

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
    "fences": {
        "unreconciled_run_grace": GRACE,
        "time_budget": "25m",
        "needs_help_after_failures": 2,
    },
}


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _config() -> dict[str, Any]:
    return json.loads(json.dumps(CONFIG))


def _write_pointer(
    home: Path,
    run_id: str,
    *,
    session: str,
    age_seconds: int = 3600,
) -> None:
    """One delivered, unreconciled run belonging to ``session``."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: delivered-node\nstatus: complete\ncommits: HEAD\n", encoding="utf-8"
    )
    terminal = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    os.utime(manifest, (terminal.timestamp(), terminal.timestamp()))
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "repo": "/temporary/repository",
            "node": {"id": "delivered-node", "plan": "plan-a", "time_budget": "20m"},
            "phase": "complete",
            "created_at": (terminal - timedelta(days=2)).isoformat(),
            "manifest_path": str(manifest),
            "log_path": str(home / "absent-stream.jsonl"),
        },
    )


class _Counted:
    """The classifier, wrapped to record the pointers the fence hands it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_ids: list[str] = []
        original = recovery_watch.classify_pointer

        def counted(record, *args, **kwargs):
            self.run_ids.append(str(record.get("run_id") or ""))
            return original(record, *args, **kwargs)

        monkeypatch.setattr(recovery_watch, "classify_pointer", counted)


def _seed(home: Path, peers: int = PEER_POINTERS) -> None:
    for index in range(peers):
        _write_pointer(home, f"r-peer-{index:03d}", session=PEER_SESSION)
    _write_pointer(home, "r-own", session=SESSION)


def test_two_hundred_peer_pointers_are_not_classified(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(crew_home)
    counter = _Counted(monkeypatch)

    overdue = recovery.overdue_unreconciled_runs(
        project=PROJECT, grace=GRACE, session=SESSION
    )

    assert counter.run_ids == ["r-own"]
    assert [row["run_id"] for row in overdue] == ["r-own"]


def test_the_project_wide_call_still_classifies_every_pointer(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(crew_home)

    counter = _Counted(monkeypatch)
    overdue = recovery.overdue_unreconciled_runs(project=PROJECT, grace=GRACE)

    assert len(counter.run_ids) == PEER_POINTERS + 1
    assert len(overdue) == len(counter.run_ids)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True
    )


@pytest.fixture()
def project_repo(tmp_path: Path, crew_home: Path) -> Path:
    """A checkout carrying one plan, so a real dispatch can resolve it."""
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
                "",
            ]
        ),
        encoding="utf-8",
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "add", "--", "skills")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _node(node_id: str):
    from reckon.crew.node import TaskNode

    return TaskNode(
        id=node_id,
        goal="start work over the fleet's queued requests",
        plan="plan-a",
        section="dispatch",
        done_when=(
            "pytest tests/test_a_dispatch_classifies_only_its_own_runs.py passes"
        ),
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return 4242

    return launch


def test_a_dispatch_classifies_only_its_own_overdue_run(
    project_repo: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(crew_home)
    counter = _Counted(monkeypatch)

    record = dispatch_module.dispatch(
        node=_node("direct"),
        project=PROJECT,
        repo=project_repo,
        config=_config(),
        session=SESSION,
        launcher=_launcher([]),
    )

    assert counter.run_ids == ["r-own"]
    backlog = runs.dispatch_field(record, "unreconciled_override")
    assert [row["run_id"] for row in backlog["waived_runs"]] == ["r-own"]


def test_a_batch_entry_classifies_only_its_own_overdue_run(
    project_repo: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(crew_home)

    node = _node("batched")
    node.manifest_path = str(project_repo.parent / "batched-manifest.md")
    counter = _Counted(monkeypatch)
    stored = dispatch_module.queue_requests(
        [node],
        project=PROJECT,
        session=SESSION,
        repo_root=project_repo,
        config=_config(),
    )

    assert counter.run_ids == ["r-own"]
    run_id = next(iter(stored))
    backlog = runs.dispatch_field(runs.read_pointer(run_id), "unreconciled_override")
    assert [row["run_id"] for row in backlog["waived_runs"]] == ["r-own"]