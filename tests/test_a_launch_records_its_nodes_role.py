"""A launch records the role of the node it was dispatched for.

Every route composes a launch from a node: a direct dispatch, a queued request
the admission pass launches, and a resume. The launch's recorded treatment is
where a reader learns whether delivery owes a review and how the work lands,
and it is composed from the role the node was dispatched with. A route that
leaves the role at its default records a review run as an implement one: it
then owes a review of its own and lands under its lane.

Each case drives the real door for its route and reads the treatment off the
composed launch, so a call site that forgets to pass the role fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import dispatch_sessions, queue_admission, runs
from reckon.crew.node import TaskNode
from reckon.hooks.install import worker_inbox_group

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
            "lane": "local-lane",
            "model_key": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

REVIEW = {
    "role": "review",
    "review_duty": False,
    "landing_treatment": "reviewed-under-its-lane",
}

IMPLEMENT = {
    "role": "implement",
    "review_duty": True,
    "landing_treatment": "promoted-under-its-lane",
}


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def harness_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the harness's hook-trust handshake without running the harness.

    Composing a launch argv asks the installed harness which inbox hook
    definition it will load, which costs a subprocess and a writable harness
    home on whichever machine the suite runs.
    """
    handshake = {
        "result": {
            "data": [
                {
                    "errors": [],
                    "hooks": [
                        {
                            "key": "/<session-flags>/config.toml:post_tool_use:0:0",
                            "currentHash": "sha256:" + "a" * 64,
                            "source": "sessionFlags",
                            "eventName": "postToolUse",
                            "handlerType": "command",
                            "command": worker_inbox_group()["hooks"][0]["command"],
                        }
                    ],
                }
            ]
        }
    }
    monkeypatch.setattr(_backends, "run_probe", lambda probe: handshake)


@pytest.fixture()
def launch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))

    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    (state / "index.json").parent.mkdir(parents=True)
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


def _lane_document(tmp_path: Path) -> Path:
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 1,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": 1}},
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _config_for() -> tuple[dict, object]:
    config = json.loads(json.dumps(CONFIG))

    def resolve(pointer):
        return config

    return config, resolve


def _planning_node(role: str, node_id: str = "node-a") -> TaskNode:
    return TaskNode(
        id=node_id,
        goal=f"launch a {role} node",
        plan="plan-a",
        section="dispatch",
        role=role,
        done_when=(
            "pytest tests/test_a_launch_records_its_nodes_role.py reports passed"
        ),
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _capturing_launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        Path(log_path).write_text('{"type":"turn.started"}\n', encoding="utf-8")
        return os.getpid()

    return launch


def _assert_treatment(plan: _backends.LaunchPlan, expected: dict) -> None:
    treatment = plan.worker_behaviour
    assert treatment.get("role") == expected["role"], (
        f"the launch recorded role {treatment.get('role')!r} for a "
        f"{expected['role']} node"
    )
    assert treatment.get("review_duty") is expected["review_duty"], (
        f"a {expected['role']} launch recorded review_duty "
        f"{treatment.get('review_duty')!r}"
    )
    assert treatment.get("landing_treatment") == expected["landing_treatment"], (
        f"a {expected['role']} launch lands as {treatment.get('landing_treatment')!r}"
    )


# ── A direct dispatch ───────────────────────────────────────────────────────


def _direct_launch(repo: Path, tmp_path: Path, role: str) -> _backends.LaunchPlan:
    from reckon.crew.dispatch import dispatch

    config, _ = _config_for()
    config["backends"]["clive"]["lane_document"] = str(_lane_document(tmp_path))
    recorded: list = []
    dispatch(
        node=_planning_node(role),
        project=PROJECT,
        repo=repo,
        config=config,
        session=SESSION,
        launcher=_capturing_launcher(recorded),
        local=True,
        watch_override=True,
        run_id=f"r-direct-{role}",
    )
    assert recorded, "the direct dispatch composed no launch"
    return recorded[-1]


def test_a_direct_launch_records_the_review_role(
    launch_repo: Path, tmp_path: Path
) -> None:
    _assert_treatment(_direct_launch(launch_repo, tmp_path, "review"), REVIEW)


def test_a_direct_launch_records_the_implement_role(
    launch_repo: Path, tmp_path: Path
) -> None:
    _assert_treatment(_direct_launch(launch_repo, tmp_path, "implement"), IMPLEMENT)


# ── A queued request the admission pass launches ───────────────────────────


def _queued_request(repo: Path, role: str, run_id: str) -> dict:
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "node": _planning_node(role).as_dict(),
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


def _admitted_launch(repo: Path, tmp_path: Path, role: str) -> _backends.LaunchPlan:
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(_lane_document(tmp_path))
    run_id = f"r-queued-{role}"
    runs.queue_dispatch(_queued_request(repo, role, run_id))
    recorded: list = []
    result = queue_admission.admit_once(
        config_for=resolve, launcher=_capturing_launcher(recorded)
    )
    assert result["launched"] == [run_id], result
    assert recorded, "the admission launch composed no launch"
    return recorded[-1]


def test_a_queued_admission_records_the_review_role(
    launch_repo: Path, tmp_path: Path
) -> None:
    _assert_treatment(_admitted_launch(launch_repo, tmp_path, "review"), REVIEW)


def test_a_queued_admission_records_the_implement_role(
    launch_repo: Path, tmp_path: Path
) -> None:
    _assert_treatment(_admitted_launch(launch_repo, tmp_path, "implement"), IMPLEMENT)


# ── A resume ───────────────────────────────────────────────────────────────


def _stopped_record(repo: Path, tmp_path: Path, role: str) -> str:
    """A cli run of ``role`` as a resume finds it: gone, with a session."""
    from reckon import crew
    from reckon.crew.runs import pointer_path
    from tests import test_a_live_run_never_reads_dead as liveness

    run_id = f"r-resume-{role}"
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "reports" / f"node-{role}" / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: node-{role}\nstatus: in-progress\n", encoding="utf-8")
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text("the original dispatch prompt\n", encoding="utf-8")
    liveness._write_exit_record(run_id)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "backend": "clive",
        "dialect": "codex",
        "role": role,
        "fenced": True,
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": f"sess-{run_id}",
        "session_harness": "codex",
        "attempt": 1,
        "base_sha": "HEAD",
        "created_at": "2026-10-01T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(prompt_path),
        "phase": "working",
        "node": {
            "id": f"node-{role}",
            "plan": "plan-a",
            "section": "dispatch",
            "role": role,
            "time_budget": "25m",
            "write_paths": [f"node-{role}.txt"],
        },
    }
    crew._write_json(pointer_path(run_id), record)
    return run_id


def _resumed_launch(
    repo: Path, tmp_path: Path, role: str, monkeypatch: pytest.MonkeyPatch
) -> _backends.LaunchPlan:
    monkeypatch.setattr(dispatch_sessions, "FENCE_WORKERS", False)
    run_id = _stopped_record(repo, tmp_path, role)
    config, _ = _config_for()
    config["backends"]["clive"]["lane_document"] = str(_lane_document(tmp_path))
    return dispatch_sessions.resume_plan(run_id, "continue", config=config)


def test_a_resumed_launch_records_the_review_role(
    launch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_treatment(
        _resumed_launch(launch_repo, tmp_path, "review", monkeypatch), REVIEW
    )


def test_a_resumed_launch_records_the_implement_role(
    launch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_treatment(
        _resumed_launch(launch_repo, tmp_path, "implement", monkeypatch), IMPLEMENT
    )
