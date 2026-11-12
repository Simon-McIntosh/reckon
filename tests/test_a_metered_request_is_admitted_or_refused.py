"""A request on a metered lane is attempted, or refused naming why.

The admission pass narrows the queue to the lane it drains. A request queued on
any other lane is dropped by that narrowing, and a dropped request is
invisible: the pass records no row for it, writes no hold on it and never
attempts a launch, so a coordinator reading it cannot tell it apart from a
request nothing has looked at yet. Three requests observed on 11 November sat
``queued`` for 25 to 60 minutes in exactly that state.

The requests below are queued by the three entry paths that were observed —
a batch entry, a queue edit and a named dispatch — onto a metered lane, in a
crew home whose resolved flight declares a metered lane and no local one, which
is the shape in which the pass excludes every metered request. One pass must
leave each with a recorded attempt naming the lane the request waits on.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, queue_admission, queue_edit, runs
from reckon.crew.node import TaskNode

dispatch = importlib.import_module("reckon.crew.dispatch")

PROJECT = "metered"
SESSION = "coordinator-metered"
LOCAL_BACKEND = "clive"
METERED_BACKEND = "codex"


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
                (
                    f"<section data-reckon={quote}section{quote}"
                    f" data-id={quote}dispatch{quote}"
                    f" data-effort-hours={quote}1.0{quote}"
                    f" data-capability-version={quote}1.0{quote}"
                    f" data-capability-class={quote}general{quote}"
                    f" data-capability-reasoning={quote}standard{quote}"
                    f" data-capability-verification={quote}strict{quote}"
                    f" data-capability-risk={quote}moderate{quote}"
                    f" data-status={quote}implementable{quote}"
                    f" data-links={quote}{quote}></section>"
                ),
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


def _lane_document(tmp_path: Path) -> str:
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 2,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": 2}},
                    "verdict": "open",
                },
            }
        ),
        encoding="utf-8",
    )
    return str(lane)


def _backend(command: str, *, lane_document: str = "") -> dict:
    entry = {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "time_budget": "25m",
    }
    if lane_document:
        entry["lane_document"] = lane_document
    return entry


def _config(tmp_path: Path, *, local_lane_declared: bool) -> dict:
    """A flight that serves a metered lane, with or without a local one.

    Without a local backend the configuration is a metered-only install: the
    admission pass's own lane is no project's local lane, so every request it
    narrows out is dropped with nothing recorded for it.
    """
    config = {
        "default_backend": METERED_BACKEND,
        "backends": {
            METERED_BACKEND: _backend("codex"),
            LOCAL_BACKEND: _backend("claude", lane_document=_lane_document(tmp_path)),
        },
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    if local_lane_declared:
        config["local_backend"] = LOCAL_BACKEND
    return config


def _node(node_id: str, tmp_path: Path) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal="admit a request queued on a metered lane",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_metered_request_is_admitted_or_refused.py passed",
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
        manifest_path=str(tmp_path / "manifests" / f"{node_id}.md"),
    )


def _queue_by_batch(repository: Path, config: dict) -> str:
    result = dispatch_batch.queue_batch(
        [
            {
                "node": "batch-node",
                "project": PROJECT,
                "plan": "plan-a",
                "section": "dispatch",
                "backend": METERED_BACKEND,
                "goal": "admit a request queued on a metered lane",
                "done_when": (
                    "pytest tests/test_a_metered_request_is_admitted_or_refused.py passed"
                ),
                "write_paths": ["batch-node.txt"],
                "time_budget": "20m",
                "spec_level": "exact",
            }
        ],
        session=SESSION,
        config_for=lambda _project: config,
        repo_root=repository,
    )
    assert result["ok"], f"the batch entry was refused: {result['results']}"
    return str(result["results"][0]["run_id"])


def _queue_by_dispatch(
    repository: Path, config: dict, tmp_path: Path, *, backend: str, node_id: str
) -> str:
    stored = dispatch.queue_requests(
        [_node(node_id, tmp_path)],
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=config,
        backend_name=backend,
    )
    assert stored, "the dispatch stored no request"
    return next(iter(stored))


def _queue_by_edit(
    repository: Path,
    config: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    run_id = _queue_by_dispatch(
        repository,
        {**config, "local_backend": LOCAL_BACKEND},
        tmp_path,
        backend=LOCAL_BACKEND,
        node_id="edited-node",
    )
    # The edit resolves the request's own flight to judge the route it is given;
    # the stub answers with the project's configuration.
    monkeypatch.setattr(
        queue_admission, "_resolve_lane_config", lambda _pointer: config
    )
    queue_edit.edit(run_id, backend=METERED_BACKEND)
    return run_id


def _attempt_of(pointer: dict) -> dict:
    """The admission attempt a queued request carries, empty when it has none."""
    attempt = {key: pointer[key] for key in ("failure", "reason") if pointer.get(key)}
    hold = queue_admission.recorded_pass_hold(pointer)
    if hold:
        attempt["pass_hold"] = hold
    elif not attempt:
        return {}
    return attempt


def test_every_metered_request_is_attempted_or_refused_naming_why(
    crew_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, local_lane_declared=False)
    queued = {
        "batch": _queue_by_batch(crew_home, config),
        "edit": _queue_by_edit(crew_home, config, tmp_path, monkeypatch),
        "dispatch": _queue_by_dispatch(
            crew_home,
            config,
            tmp_path,
            backend=METERED_BACKEND,
            node_id="named-node",
        ),
    }
    for run_id in queued.values():
        pointer = runs.read_pointer(run_id)
        assert pointer["phase"] == "queued", (
            f"{run_id} must be queued before the pass, but reads {pointer['phase']!r}"
        )
        assert pointer.get("backend") == METERED_BACKEND, (
            f"{run_id} is queued on {pointer.get('backend')!r}, not the metered lane"
        )

    launched: list = []

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched.append(plan)
        return 0

    result = queue_admission.admit_once(
        config_for=lambda _pointer: config,
        launcher=launcher,
        lane=LOCAL_BACKEND,
    )

    for path, run_id in queued.items():
        pointer = runs.read_pointer(run_id)
        attempt = _attempt_of(pointer)
        assert attempt, (
            f"the {path}-queued request {run_id} carries no admission attempt: the "
            f"pass left it queued with nothing recorded (pass verdicts: "
            f"{[row.get('run_id') for row in result['verdicts']]})"
        )
        recorded = attempt.get("pass_hold") or {}
        reason = str(
            recorded.get("reason")
            or attempt.get("reason")
            or attempt.get("failure")
            or ""
        )
        assert METERED_BACKEND in reason, (
            f"the {path}-queued request's recorded attempt names no lane: {reason!r}"
        )
    assert launched == [], (
        "no launcher in this configuration may run a request the drainer's lane "
        f"does not serve, but {len(launched)} were launched"
    )
