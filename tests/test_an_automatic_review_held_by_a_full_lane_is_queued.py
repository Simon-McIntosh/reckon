"""An automatic review held by a full, unpaused local lane waits as a queued run.

The reflex composes a delivered run's review itself rather than through the CLI,
and both surfaces must read a full lane the same way: a lane whose router grants
this session no worker queues the request for the admission pass, and only a
paused gate holds it with nothing queued. Measured on 2026-10-24 at 06:44Z: the
reflex answered a delivered run's review with ``error: lane-paused`` and no
``queued`` key while the gate was unpaused, and one minute later
``reckon crew dispatch --role review`` queued the same review at position 1. The
run then waited for the reflex to fire again rather than for a slot.

Every environment-resolved directory and both gate documents point at temporary
paths, so nothing here reads or writes the fleet's own crew home or lane.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew
from reckon.crew import recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
SESSION = "session-orchestrating"
LOCAL_BACKEND = "clive"
METERED_BACKEND = "codex"


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


def _write_json(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _full_lane_document(tmp_path: Path) -> str:
    """The lane's own reading of a full, unpaused lane: no slot for the session."""
    return _write_json(
        tmp_path / "lane.json",
        {
            "headroom": 0,
            "admission": {
                "worker_slots": 0,
                "new_session_worker_slots": 0,
                "observed_seconds": 900,
                "sessions": {SESSION: {"worker_slots": 0}},
                "verdict": "full",
            },
            "observed_at": "2026-10-24T06:40:00Z",
        },
    )


def _gate_document(tmp_path: Path, *, paused: bool) -> str:
    return _write_json(
        tmp_path / ("gate-paused.json" if paused else "gate-open.json"),
        {"paused": paused} if paused else {"paused": False},
    )


def _config(*, lane_document: str, gate_document: str) -> dict:
    return {
        "default_backend": LOCAL_BACKEND,
        "local_backend": LOCAL_BACKEND,
        "backends": {
            LOCAL_BACKEND: {
                **_backend("claude"),
                "lane_document": lane_document,
                "gate_document": gate_document,
            },
            METERED_BACKEND: _backend("codex"),
        },
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">An automatic review held by a full lane is queued</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _release_watcher() -> None:
    if crew.watch_state(PROJECT)["watcher_live"]:
        recovery.unwatch(PROJECT)
        _wait_for_stopped_producer()


def _delivered_pointer(config_home: Path, repo: Path, run_id: str) -> dict:
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\ncommits: " + run_id + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "node": {
            "id": run_id,
            "plan": "fixture",
            "section": "s2",
            "lane_declaration": {"backend": LOCAL_BACKEND},
        },
        "backend": LOCAL_BACKEND,
        "local": True,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": SESSION,
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _queued_pointers() -> list[dict]:
    return [pointer for pointer in runs.list_live() if pointer.get("phase") == "queued"]


def _review_pointers() -> list[dict]:
    return [
        pointer
        for pointer in runs.list_live()
        if str((pointer.get("node") or {}).get("id", "")).startswith(
            recovery.REVIEW_NODE_PREFIX
        )
    ]


def test_a_full_unpaused_lane_queues_the_review(
    tmp_path: Path, isolated_project: tuple[Path, Path], monkeypatch
) -> None:
    config_home, repo = isolated_project
    config = _config(
        lane_document=_full_lane_document(tmp_path),
        gate_document=_gate_document(tmp_path, paused=False),
    )
    record = _delivered_pointer(config_home, repo, "r-delivered")
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    real_dispatch = dispatch_module.dispatch
    allowance_holds: list[dict] = []

    def held_without_a_pointer(**kwargs):
        """The hold the reflex met at 06:44Z: the router's decision, no pointer.

        The dispatch's own queueing writes the pointer onto the exception, so a
        hold that arrives without one is the shape the recorded failure had.
        Raising it here reproduces that record rather than a hand-built stand-in
        for it, and the reflex must still leave the review queued.
        """
        settings = (kwargs["config"].get("backends") or {}).get("clive") or {}
        allowance = dispatch_module._dispatch_lane_allowance(
            settings, session=kwargs.get("session") or ""
        )
        allowance_holds.append(dict(allowance))
        raise dispatch_module.LaneHeld(allowance)

    monkeypatch.setattr(dispatch_module, "dispatch", held_without_a_pointer)
    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            report = recovery.dispatch_review_for_run(
                record,
                config=config,
                launcher=lambda *a, **k: os.getpid(),
            )
    finally:
        monkeypatch.setattr(dispatch_module, "dispatch", real_dispatch)
        _release_watcher()

    assert allowance_holds and allowance_holds[0]["held"] is True, (
        "the fixture must raise a real router hold, not a shut gate: "
        + json.dumps(allowance_holds, default=str)
    )

    assert report["dispatched"] is False
    queued = report.get("queued")
    assert queued, (
        "a full, unpaused local lane must leave a queued review run; report="
        + json.dumps(report, default=str)
    )
    queued_records = _queued_pointers()
    assert len(queued_records) == 1, (
        "the reflex left no queued pointer for the held review: "
        + json.dumps(queued_records, default=str)
    )
    assert queued_records[0]["run_id"] == queued["run_id"]
    assert queued_records[0]["backend"] == LOCAL_BACKEND
    assert str((queued_records[0].get("node") or {}).get("id", "")).startswith(
        recovery.REVIEW_NODE_PREFIX
    )


def test_a_paused_gate_queues_nothing(
    tmp_path: Path, isolated_project: tuple[Path, Path]
) -> None:
    config_home, repo = isolated_project
    config = _config(
        lane_document=_full_lane_document(tmp_path),
        gate_document=_gate_document(tmp_path, paused=True),
    )
    record = _delivered_pointer(config_home, repo, "r-paused")
    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            report = recovery.dispatch_review_for_run(
                record,
                config=config,
                launcher=lambda *a, **k: os.getpid(),
            )
    finally:
        _release_watcher()

    assert report["dispatched"] is False
    assert report["error"] == "lane-paused"
    assert "queued" not in report
    assert _queued_pointers() == []
    assert _review_pointers() == []


def test_the_cli_queues_the_same_request_at_the_same_position(
    tmp_path: Path, isolated_project: tuple[Path, Path]
) -> None:
    config_home, _repo = isolated_project
    config = _config(
        lane_document=_full_lane_document(tmp_path),
        gate_document=_gate_document(tmp_path, paused=False),
    )
    home = tmp_path / "home"
    home.mkdir()
    custom = _write_json(tmp_path / "flight-config.json", config)

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            "fixture",
            "--section",
            "s2",
            "--role",
            "review",
            "--spec-level",
            "exact",
            "--node",
            "review-of-fixture",
            "--goal",
            "review the fixture section",
            "--done-when",
            "`reckon --version` prints a version and EXIT=0",
            "--write-path",
            "reckon/crew/fixture_review_note.py",
            "--session",
            SESSION,
            "--local",
        ],
        env={
            **os.environ,
            "HOME": str(home),
            "RECKON_HOME": str(config_home),
            "RECKON_FLIGHT_CONFIG": custom,
        },
    )
    assert result.exit_code == 0, (result.output, result.exception)
    payload = json.loads(result.output)
    assert payload["queued"] is True
    assert payload["position"] == 1
    assert payload["run_id"]
