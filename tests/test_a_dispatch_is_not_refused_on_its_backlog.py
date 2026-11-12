"""A session's landing backlog is recorded on its runs, never enforced.

A coordinator can fall behind on reconciling the runs it delivered. Those runs
are duties it owes, and the obligations view lists them; they do not hold new
work back, so a direct dispatch and a batch entry both go through and both
name, on the record they write, the overdue runs the work was chosen over.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import obligations, runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
SESSION = "coordinator-a"

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
        "time_budget": "25m",
        "needs_help_after_failures": 2,
        "unreconciled_run_grace": "5m",
    },
}


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def project_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

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
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "add", "--", "skills")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _config() -> dict:
    return json.loads(json.dumps(CONFIG))


def _node(node_id: str):
    from reckon.crew.node import TaskNode

    return TaskNode(
        id=node_id,
        goal="start work over an unreconciled backlog",
        plan="plan-a",
        section="dispatch",
        done_when=(
            "pytest tests/test_a_dispatch_is_not_refused_on_its_backlog.py passes"
        ),
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _write_overdue_run(home: Path, run_id: str, *, age_seconds: int = 3600) -> None:
    """Write one delivered run of this session, older than the grace."""
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
            "session": SESSION,
            "repo": "/temporary/repository",
            "node": {
                "id": "delivered-node",
                "plan": "plan-a",
                "time_budget": "20m",
            },
            "phase": "complete",
            "created_at": (terminal - timedelta(days=2)).isoformat(),
            "manifest_path": str(manifest),
            "log_path": str(home / "absent-stream.jsonl"),
        },
    )


def _launcher(recorded: list):
    # A pid no process holds: the dispatch writes its record and the worker it
    # names is gone, so the run can be completed without a live process in the
    # way.
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return 4242

    return launch


def test_a_direct_dispatch_runs_over_the_backlog_and_records_it(
    project_repo: Path,
) -> None:
    home = Path(os.environ["RECKON_HOME"])
    _write_overdue_run(home, "r-delivered")
    recorded: list = []

    record = dispatch_module.dispatch(
        node=_node("direct"),
        project=PROJECT,
        repo=project_repo,
        config=_config(),
        session=SESSION,
        launcher=_launcher(recorded),
    )

    assert recorded, "the dispatch launched no worker"
    run_id = str(record["run_id"])
    backlog = runs.dispatch_field(runs.read_pointer(run_id), "unreconciled_override")
    assert [row["run_id"] for row in backlog["waived_runs"]] == ["r-delivered"]
    assert backlog["requested"] is False
    # The same runs ride the committed row, so the backlog survives the pointer.
    committed = crew.complete(run_id, gate="passed")["record"]
    assert [
        row["run_id"] for row in committed["unreconciled_override"]["waived_runs"]
    ] == ["r-delivered"]


def test_a_batch_entry_runs_over_the_backlog_and_records_it(
    project_repo: Path,
) -> None:
    """The batch's queue writer stores the backlog on the request it queues."""
    home = Path(os.environ["RECKON_HOME"])
    _write_overdue_run(home, "r-delivered")

    node = _node("batched")
    node.manifest_path = str(project_repo.parent / "batched-manifest.md")
    stored = dispatch_module.queue_requests(
        [node],
        project=PROJECT,
        session=SESSION,
        repo_root=project_repo,
        config=_config(),
    )

    run_id = next(iter(stored))
    backlog = runs.dispatch_field(runs.read_pointer(run_id), "unreconciled_override")
    assert [row["run_id"] for row in backlog["waived_runs"]] == ["r-delivered"]


def test_a_session_with_no_backlog_records_none(project_repo: Path) -> None:
    _write_overdue_run(Path(os.environ["RECKON_HOME"]), "r-delivered", age_seconds=30)

    record = dispatch_module.dispatch(
        node=_node("direct"),
        project=PROJECT,
        repo=project_repo,
        config=_config(),
        session=SESSION,
        launcher=_launcher([]),
    )

    assert runs.dispatch_field(record, "unreconciled_override") is None


def test_the_overdue_run_stays_a_listed_obligation(project_repo: Path) -> None:
    home = Path(os.environ["RECKON_HOME"])
    _write_overdue_run(home, "r-delivered")

    dispatch_module.dispatch(
        node=_node("direct"),
        project=PROJECT,
        repo=project_repo,
        config=_config(),
        session=SESSION,
        launcher=_launcher([]),
    )

    listed = obligations.obligations(PROJECT, SESSION)["obligations"]
    assert [row["run_id"] for row in listed if row["run_id"] == "r-delivered"], listed
