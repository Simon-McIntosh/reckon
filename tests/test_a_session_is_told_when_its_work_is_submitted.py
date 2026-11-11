"""A session is told, in the pass that submits its queued request, that it began.

A coordinator queues many sections in one beat and the admit service submits
them as slots free, possibly hours later. The tick that announces a start is
not the same moment as the launch, so the admit pass writes one row of its own
kind to the coordinating session's watch stream: it names the run, the plan
section, the lane and model the worker started on, the advice it carried and
the ``crew tell`` command that reaches it. A request belonging to another
session writes nothing to this one, a launch with no session to address still
launches, and a pass that launches nothing writes no row.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode

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
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "skills" / "reckon-build" / "scripts"
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


def _lane_document(tmp_path: Path, *, worker_slots: float) -> Path:
    lane = tmp_path / f"lane-{worker_slots}.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": worker_slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": worker_slots}},
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _config_for(lane_document: Path):
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["clive"]["lane_document"] = str(lane_document)

    def resolve(_pointer):
        return config

    return resolve


def _request(
    repository: Path,
    *,
    node_id: str,
    run_id: str,
    session: str = SESSION,
    section: str = "dispatch",
    comment: str = "",
) -> dict:
    node = TaskNode(
        id=node_id,
        goal="admit this queued node",
        plan="plan-a",
        section=section,
        done_when="pytest reports passed",
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    pointer: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
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
            "section": section,
            "session": session,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }
    if comment:
        pointer["dispatch_options"]["comment"] = comment
    return pointer


def _stub_dispatch(**kwargs):
    """The record a worker start returns, without starting a worker."""
    node = kwargs["node"]
    return {
        "run_id": str(kwargs["run_id"]),
        "project": str(kwargs["project"]),
        "session": str(kwargs["session"]),
        "phase": "working",
        "node": node.as_dict(),
        "lane": "local-lane",
        "model_key": "local-model",
        "agent": {"model": "local-model", "effort": "medium"},
    }


def _stub_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch_module, "dispatch", _stub_dispatch)


def _submission_rows() -> list[dict]:
    path = runs.watch_stream_path(PROJECT)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = runs.parse_stream_line(line)
        if event is not None and event.get("event") == "submission":
            rows.append(event)
    return rows


def test_two_queued_requests_of_one_session_write_two_rows(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = _config_for(_lane_document(tmp_path, worker_slots=2))
    runs.queue_dispatch(_request(admit_repo, node_id="first-node", run_id="r-first"))
    runs.queue_dispatch(_request(admit_repo, node_id="second-node", run_id="r-second"))
    steering = runs.run_dir("r-first")
    steering.mkdir(parents=True, exist_ok=True)
    (steering / runs.STEERING_FILE).write_text(
        "steer the first request left, not right\n", encoding="utf-8"
    )
    _stub_launch(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert sorted(result["launched"]) == ["r-first", "r-second"]
    rows = _submission_rows()
    assert len(rows) == 2
    by_run = {row["run_id"]: row for row in rows}
    assert set(by_run) == {"r-first", "r-second"}
    for run_id, row in by_run.items():
        assert row["session"] == SESSION
        assert row["plan"] == "plan-a"
        assert row["section"] == "dispatch"
        assert row["lane"] == "local-lane"
        assert row["model"] == "local-model"
        assert row["tell_command"].startswith(f"reckon crew tell --run {run_id}")
        assert row["tell_command"] in row["detail"]
    assert (
        by_run["r-first"]["advice"].strip() == "steer the first request left, not right"
    )


def test_another_sessions_request_writes_nothing_to_this_one(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = _config_for(_lane_document(tmp_path, worker_slots=2))
    runs.queue_dispatch(_request(admit_repo, node_id="mine", run_id="r-mine"))
    runs.queue_dispatch(
        _request(
            admit_repo,
            node_id="theirs",
            run_id="r-theirs",
            session="coordinator-b",
        )
    )
    _stub_launch(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert sorted(result["launched"]) == ["r-mine", "r-theirs"]
    rows = _submission_rows()
    # The other session's row exists, so a pane scoped to this one would be
    # able to tell the two apart rather than see nothing at all.
    assert [row["run_id"] for row in rows if row["session"] == "coordinator-b"] == [
        "r-theirs"
    ]
    mine = [row for row in rows if row["session"] == SESSION]
    assert [row["run_id"] for row in mine] == ["r-mine"]


def test_a_launch_with_no_session_still_launches_and_writes_no_row(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = _config_for(_lane_document(tmp_path, worker_slots=2))
    runs.queue_dispatch(
        _request(admit_repo, node_id="unowned", run_id="r-unowned", session="")
    )
    runs.queue_dispatch(_request(admit_repo, node_id="owned", run_id="r-owned"))
    _stub_launch(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert sorted(result["launched"]) == ["r-owned", "r-unowned"]
    # The addressed request's row proves the launch path writes at all, so the
    # unaddressed request's silence is its own fact rather than a dead reader.
    assert [row["run_id"] for row in _submission_rows()] == ["r-owned"]


def test_a_pass_that_launches_nothing_writes_no_row(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve = _config_for(_lane_document(tmp_path, worker_slots=0))
    runs.queue_dispatch(_request(admit_repo, node_id="waiting", run_id="r-waiting"))
    _stub_launch(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert result["launched"] == []
    assert result["held"] == ["r-waiting"]
    assert _submission_rows() == []
