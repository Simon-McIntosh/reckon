"""A review row reads the producer's lease, and a compose without one queues."""

from __future__ import annotations

import importlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from reckon.crew import obligations, runs
from reckon.crew import recovery_review_dispatch as recovery
from reckon.crew.node import WatcherRequired

PROJECT = "producer-liveness-fixture"
SESSION = "producer-liveness-session"
RUN_ID = "run-owing-a-review"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _backend(command: Path) -> dict:
    return {
        "launch": "cli",
        "command": str(command),
        "model": "review-model",
        "time_budget": "25m",
    }


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="' + PROJECT + '">',
        encoding="utf-8",
    )
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "-A"),
        ("commit", "-q", "-m", "chore: seed the fixture"),
    ):
        _git(repo, *args)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    head = _git(repo, "rev-parse", "HEAD")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "codex"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    config = {
        "default_backend": "text-lane",
        "local_backend": "text-lane",
        "backends": {"text-lane": _backend(command)},
        "roles": {
            "implement": {},
            "review": {},
        },
    }
    manifest = home / "manifests" / (RUN_ID + ".md")
    manifest.parent.mkdir()
    manifest.write_text(
        "node: " + RUN_ID + "\nstatus: complete\ncommits: [" + head + "]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": head,
        "backend": "text-lane",
        "launch": "cli",
        "phase": "starting",
        "process_alive": False,
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": RUN_ID,
            "plan": "fixture",
            "section": "overview",
            "time_budget": "25m",
            "write_paths": ["docs/plans/fixture.html"],
        },
    }
    return {
        "home": home,
        "repo": repo,
        "head": head,
        "config": config,
        "pointer": pointer,
    }


def _renew_lease(project: dict, *, silence: float) -> None:
    runs.update_watch_registration(
        PROJECT, lease_renewed_at=time.time() - silence, host="fixture-host"
    )


def _review_duty() -> dict:
    result = obligations.obligations(PROJECT, SESSION)
    duties = [
        item for item in result["obligations"] if item["kind"] == "review-missing"
    ]
    assert len(duties) == 1, [
        (item["kind"], item.get("run_id")) for item in result["obligations"]
    ]
    return duties[0]


def _publish(project: dict) -> None:
    runs._write_json(runs.pointer_path(RUN_ID), project["pointer"])


def test_a_row_with_no_producer_states_the_producer_is_down(project: dict) -> None:
    """No lease renewal for a full interval reads as a producer that is down."""
    _publish(project)
    silence = int(runs.producer_lease_seconds()) + 60
    _renew_lease(project, silence=silence)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        duty = _review_duty()

    command = str(duty["next_command"])
    assert "the producer is down" in command, duty
    assert obligations._overdue_figure(silence) in command, duty
    assert "reckon crew watch --ensure --project " + PROJECT in command, duty
    assert "expected within" not in command, duty
    assert "review_owner" not in duty, duty
    assert "reflex_window_seconds" not in duty, duty


def test_a_row_with_a_live_producer_still_promises_the_sweep(project: dict) -> None:
    """A lease renewed inside its interval keeps the reflex as the owner."""
    _publish(project)
    _renew_lease(project, silence=1)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        duty = _review_duty()

    command = str(duty["next_command"])
    assert duty["review_owner"] == "reflex", duty
    assert duty["reflex_window_seconds"] == obligations._reflex_review_window_seconds()
    assert "expected within" in command, duty
    assert "the producer is down" not in command, duty


def test_a_review_composed_without_a_producer_is_queued(
    project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused composition is the queue's request, not a dark brief."""
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    _publish(project)
    record = dict(project["pointer"])
    ensure = "reckon crew watch --ensure --project " + PROJECT

    def refuse_watch_required(**kwargs: object) -> dict:
        raise WatcherRequired(PROJECT, {"watcher_live": False, "ensure_line": ensure})

    monkeypatch.setattr(dispatch_module, "dispatch", refuse_watch_required)
    report = recovery.dispatch_review_for_run(record, config=project["config"])

    assert report["dispatched"] is False, report
    queued = report["queued"]
    assert queued["run_id"]
    listed = [
        row["run_id"]
        for row in runs._list_live_records(project=PROJECT, phase="queued")
    ]
    assert queued["run_id"] in listed, listed
    assert ensure in str(queued["reason"])
