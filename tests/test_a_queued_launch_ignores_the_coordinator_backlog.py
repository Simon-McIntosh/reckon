"""A launch from the queue is not refused on its coordinator's backlog.

Queued work exists so that it runs when its lane frees, whether or not its
coordinator is awake to reconcile delivered runs. Neither route is held back by
a session's terminal backlog. A coordinator's direct dispatch records the
overdue runs the work was chosen over; admission continues a queued request
without computing or recording that backlog.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs

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


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "add", "--", "skills")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _config_for():
    config = json.loads(json.dumps(CONFIG))

    def resolve(_pointer):
        return config

    return config, resolve


def _node(node_id: str):
    from reckon.crew.node import TaskNode

    return TaskNode(
        id=node_id,
        goal="launch this node from the queue",
        plan="plan-a",
        section="dispatch",
        done_when=(
            "pytest tests/test_a_queued_launch_ignores_the_coordinator_backlog.py passes"
        ),
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _queue(repository: Path, *, node_id: str, run_id: str) -> dict:
    node = _node(node_id)
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
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
        "hold": {"held": True},
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


def _write_overdue_run(home: Path, run_id: str, *, age_seconds: int = 3600) -> None:
    """Write one delivered run of this session, older than the grace."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True)
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
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def test_an_admission_launch_waives_its_coordinator_backlog(
    admit_repo: Path,
) -> None:
    home = Path(os.environ["RECKON_HOME"])
    _write_overdue_run(home, "r-delivered")
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="queued", run_id="r-queued"))
    recorded: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert result["launched"] == ["r-queued"], json.dumps(result, default=str)
    pointer = runs.read_pointer("r-queued")
    waiver = runs.dispatch_field(pointer, "unreconciled_override")
    assert waiver is None


def test_a_direct_dispatch_records_the_backlog_it_runs_over(
    admit_repo: Path,
) -> None:
    """A direct dispatch succeeds and names the runs it was chosen over."""
    home = Path(os.environ["RECKON_HOME"])
    _write_overdue_run(home, "r-delivered")
    config, _ = _config_for()
    recorded: list = []

    result = dispatch_module.dispatch(
        node=_node("direct"),
        project=PROJECT,
        repo=admit_repo,
        config=config,
        session=SESSION,
        launcher=_launcher(recorded),
    )

    assert result["run_id"] != "r-delivered"
    backlog = runs.dispatch_field(result, "unreconciled_override")
    assert backlog["source"] == "coordinator"
    assert backlog["requested"] is False
    assert [row["run_id"] for row in backlog["waived_runs"]] == ["r-delivered"]


def test_a_request_with_no_backlog_records_no_waiver(admit_repo: Path) -> None:
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="clean", run_id="r-clean"))

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert result["launched"] == ["r-clean"], json.dumps(result, default=str)
    assert (
        runs.dispatch_field(runs.read_pointer("r-clean"), "unreconciled_override")
        is None
    )


def test_the_stored_override_does_not_govern_the_admission_launch(
    admit_repo: Path,
) -> None:
    """The value recorded when the request was queued does not decide its launch.

    A request can wait days for a lane, so the waiver its dispatch stored then
    describes that moment. Admission continues the request without computing
    or recording its session's backlog at launch.
    """
    home = Path(os.environ["RECKON_HOME"])
    _write_overdue_run(home, "r-delivered")
    _, resolve = _config_for()
    request = _queue(admit_repo, node_id="stored", run_id="r-stored")
    request["dispatch_options"]["allow_unreconciled_runs"] = False
    runs.queue_dispatch(request)

    result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

    assert result["launched"] == ["r-stored"], json.dumps(result, default=str)
    waiver = runs.dispatch_field(runs.read_pointer("r-stored"), "unreconciled_override")
    assert waiver is None
