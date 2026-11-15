"""A stranded review pointer does not stand for its reviewed run.

A review's pointer alone used to stand for the review. A pointer at a pre-spawn
phase with no pid, no worker record, no stream and no launch log, past the
bound, is a launch that never spawned: nothing is coming to finish it and
nothing is coming to end it, so the reviewed run's row reported a review in
flight forever and its remedy pointed at a run that could never file a record.
The row now reports the run as owing a review, and the remedy it prints is the
review dispatch, so the coordinator's next move releases the pointer and
composes the review again.

The dispatch path itself is not changed here: it releases the stranded claim at
``dispatch_review_for_run`` on the recogniser's reading alone, so this file pins
both halves: the row that stops counting the stranded review as in flight, and
the dispatch that discards the pointer and reaches its launch.

A review inside the bound, or one whose launch wrote any evidence, still stands.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, recovery_review_dispatch, runs

PROJECT = "stranded-review-fixture"
SESSION = "coordinator-fixture"
SOURCE_RUN = "r-scoring-source"
REVIEW_RUN = "r-review-stranded"
NOW = 1_800_000_000.0

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "fixture",
            "effort": "high",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised crew home; the fixture never touches the operator's."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    (config_home / "live").mkdir()
    return config_home


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _review_pointer(
    review_run_id: str, *, started_at: str, worker_record: bool = False
) -> dict[str, Any]:
    """A review pointer holding a pre-spawn phase and no launch evidence.

    ``worker_record`` writes the one piece of launch evidence a spawned worker
    leaves behind, which is what separates a young launch from a stranded one.
    """
    directory = runs.run_dir(review_run_id)
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "starting",
        "created_at": started_at,
        "attempt_started_at": started_at,
    }
    runs._write_json(runs.pointer_path(review_run_id), record)
    if worker_record:
        runs._write_json(directory / "worker.json", {"pid": 1, "host": "fixture"})
    return record


def _scoring_pointer(
    crew_home: Path, review_run_id: str, *, repo: Path | None = None, head: str = ""
) -> dict[str, Any]:
    """A completed run whose dispatch record names the review it was given."""
    manifest = crew_home / "manifests" / f"{SOURCE_RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    commits = f"[{head}]" if head else "none"
    manifest.write_text(
        f"node: {SOURCE_RUN}\nstatus: complete\ncommits: {commits}\n",
        encoding="utf-8",
    )
    record: dict[str, Any] = {
        "run_id": SOURCE_RUN,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "manifest_path": str(manifest),
        "role": "implement",
        "node": {
            "id": "source-node",
            "plan": "fixture-plan",
            "section": "s",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
        recovery.REVIEW_DISPATCH_FIELD: {
            "run_id": review_run_id,
            "status": "dispatched",
            "head": "",
        },
    }
    if repo is not None:
        record["repo"] = str(repo)
        record["worktree"] = str(repo)
        record["base_sha"] = head
        record["backend"] = "alpha"
    runs._write_json(runs.pointer_path(SOURCE_RUN), record)
    return record


def _repository(tmp_path: Path) -> tuple[Path, str]:
    """A repository and its head, so a composed review has a revision to key on."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("config", "user.email", "worker@example.invalid"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, head


def test_a_stranded_review_does_not_stand_for_its_run(crew_home: Path) -> None:
    """The row stops reporting a review in flight and prints the review dispatch."""
    _scoring_pointer(crew_home, REVIEW_RUN)
    _review_pointer(
        REVIEW_RUN,
        started_at=_at(NOW - recovery.STRANDED_LAUNCH_BOUND_SECONDS - 60),
    )

    row = recovery.classify_pointer(runs.read_pointer(SOURCE_RUN), now_seconds=NOW)

    assert row["classification"] == "scoring"
    assert "in flight" not in row["detail"]
    assert "no independent review is attached" in row["detail"]
    assert row["next_action"].startswith("reckon crew dispatch")
    assert "--role review" in row["next_action"]
    assert SOURCE_RUN in row["next_action"]


def test_the_review_dispatch_discards_the_stranded_pointer_and_launches(
    crew_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The printed remedy is a command that releases the pointer and launches."""
    repo, head = _repository(tmp_path)
    _scoring_pointer(crew_home, REVIEW_RUN, repo=repo, head=head)
    _review_pointer(
        REVIEW_RUN,
        started_at=_at(time.time() - 2 * recovery.STRANDED_LAUNCH_BOUND_SECONDS),
    )
    # The composed review is refused where it would dispatch from a fixture home;
    # what this test measures is that the recompose is reached at all, because
    # the standing stranded claim no longer returns in flight.
    monkeypatch.setattr(
        recovery_review_dispatch, "carry_review_forward", lambda *a, **k: None
    )
    launched: list[dict[str, Any]] = []
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    def fake_dispatch(**kwargs: Any) -> dict[str, Any]:
        launched.append(kwargs)
        return {"run_id": "r-recomposed"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)

    report = recovery.dispatch_review_for_run(
        runs.read_pointer(SOURCE_RUN),
        config=CONFIG,
        launcher=lambda *a, **k: 1234,
    )

    assert report["dispatched"] is True
    assert len(launched) == 1
    assert not runs.pointer_path(REVIEW_RUN).exists()
    assert recovery._review_in_flight(runs.read_pointer(SOURCE_RUN)) == ""
    marker = json.loads(
        (runs.run_dir(REVIEW_RUN) / "discard.json").read_text(encoding="utf-8")
    )
    assert marker["run_id"] == REVIEW_RUN
    assert marker["phase"] == "starting"
    assert "stranded launch" in marker["reason"]


def test_a_review_younger_than_the_bound_is_still_in_flight(crew_home: Path) -> None:
    _scoring_pointer(crew_home, REVIEW_RUN)
    _review_pointer(REVIEW_RUN, started_at=_at(NOW - 60))

    row = recovery.classify_pointer(runs.read_pointer(SOURCE_RUN), now_seconds=NOW)

    assert row["classification"] == "scoring"
    assert f"in flight as {REVIEW_RUN}" in row["detail"]
    assert f"in flight as {REVIEW_RUN}" in row["next_action"]


def test_a_review_with_a_worker_record_is_still_in_flight(crew_home: Path) -> None:
    """Launch evidence written is the fact that separates young from stranded."""
    _scoring_pointer(crew_home, REVIEW_RUN)
    _review_pointer(
        REVIEW_RUN,
        started_at=_at(NOW - recovery.STRANDED_LAUNCH_BOUND_SECONDS - 60),
        worker_record=True,
    )

    row = recovery.classify_pointer(runs.read_pointer(SOURCE_RUN), now_seconds=NOW)

    assert row["classification"] == "scoring"
    assert f"in flight as {REVIEW_RUN}" in row["detail"]
