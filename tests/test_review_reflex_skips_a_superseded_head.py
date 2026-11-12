"""The sweep reviews only a delivery the run's current attempt has settled."""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reckon.crew import recovery_review_acceptance as acceptance
from reckon.crew import recovery_review_dispatch as reviews
from reckon.crew import resumption, runs
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")
PROJECT = "review-ordering-fixture"
SESSION = "fixture-coordinator"
RUN = "r-review-source"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "value.py").write_text("VALUE = 1\n")
    git(repo, "add", "value.py")
    git(repo, "commit", "-qm", "feat: supply a value", "-m", "Provide fixture source.")
    head = git(repo, "rev-parse", "HEAD")
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    manifest = home / "manifest.md"
    manifest.write_text(f"node: fixture\nstatus: complete\ncommits: [{head}]\n")
    record = {
        "run_id": RUN,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "manifest_path": str(manifest),
        "phase": "working",
        "launch": "cli",
        "role": "implement",
        "process_alive": False,
        "node": {"id": "fixture", "write_paths": ["value.py"]},
    }
    runs._write_json(runs.pointer_path(RUN), record)
    heads = []

    def compose(record, **kwargs):
        reviewed_head = reviews._run_head_for_review(record)
        heads.append(reviewed_head)
        return {"run_id": RUN, "dispatched": True, "review_run_id": "r-fixture-review"}

    monkeypatch.setattr(reviews, "_dispatch_text_review_for_run", compose)
    monkeypatch.setattr(reviews, "_changed_figure_paths", lambda record: [])
    monkeypatch.setattr(acceptance, "dispatch_repair_for_run", lambda *a, **k: {})
    monkeypatch.setattr(
        resumption.plan_review, "compose_owed_plan_reviews", lambda *a, **k: {}
    )
    return SimpleNamespace(
        home=home, repo=repo, manifest=manifest, head=head, heads=heads
    )


def _sweep(pointer=None):
    report = resumption.sweep(
        PROJECT,
        session=SESSION,
        config={},
        pointers=[runs.read_pointer(RUN) if pointer is None else pointer],
    )
    assert not report["reviews"].get("error"), report
    assert not report["reviews"]["failed"], report
    return report["reviews"]


def test_a_sweep_snapshot_from_before_resume_dispatches_no_superseded_review(
    world, monkeypatch
):
    snapshot = runs.read_pointer(RUN)
    assert acceptance.classify_pointer(snapshot)["classification"] == "scoring"

    class WindowObservedError(Exception):
        pass

    def compose_resume(*args, **kwargs):
        current = runs.read_pointer(RUN)
        assert resumption.RESUME_LAUNCH_IN_FLIGHT in current
        assert reviews.RESUME_SUPERSEDED_HEAD not in current
        _sweep(snapshot)
        raise WindowObservedError

    monkeypatch.setattr(resumption, "resume_plan", compose_resume)
    with pytest.raises(WindowObservedError):
        resumption._resume(
            RUN, snapshot, config={}, launcher=lambda *a, **k: os.getpid()
        )
    assert world.heads == [], f"superseded head dispatched: {world.heads}"


def test_a_live_process_between_classification_and_dispatch_gets_no_review(
    world, monkeypatch
):
    snapshot = runs.read_pointer(RUN)
    classify = acceptance.classify_pointer

    def classify_then_start(record, **kwargs):
        result = classify(record, **kwargs)
        assert result["classification"] == "scoring"
        current = runs.read_pointer(RUN)
        current["pid"] = os.getpid()
        runs._write_json(runs.pointer_path(RUN), current)
        return result

    monkeypatch.setattr(acceptance, "classify_pointer", classify_then_start)
    report = _sweep(snapshot)
    assert world.heads == []
    assert report["refused"]


def test_a_deferral_withholds_review_until_its_instant(world, monkeypatch):
    moment = datetime.now(UTC)
    until = moment + timedelta(hours=1)
    record = runs.read_pointer(RUN)
    record["acknowledgement"] = {
        "until": until.isoformat(),
        "reason": "wait for delivery",
    }
    runs._write_json(runs.pointer_path(RUN), record)
    assert RUN in obligations._acknowledgements_in_force(PROJECT, now=moment)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(reviews, "datetime", Clock)
    report = _sweep()
    assert world.heads == []
    assert report["refused"]
    moment = until - timedelta(microseconds=1)
    _sweep()
    assert world.heads == []
    moment = until
    _sweep()
    assert world.heads == [world.head]


def test_a_resumed_run_that_delivers_its_new_head_gets_one_review(world):
    snapshot = runs.read_pointer(RUN)
    resumption._record_resume_supersedes_head(RUN)
    record = runs.read_pointer(RUN)
    record["manifest_baseline_mtime_ns"] = world.manifest.stat().st_mtime_ns
    record[reviews.RESUME_SUPERSEDED_HEAD]["at"] = (
        datetime.now(UTC) - timedelta(minutes=10)
    ).isoformat()
    runs._write_json(runs.pointer_path(RUN), record)
    held = _sweep(snapshot)
    assert world.heads == []
    assert held["refused"]
    (world.repo / "value.py").write_text("VALUE = 2\n")
    git(world.repo, "add", "value.py")
    git(
        world.repo,
        "commit",
        "-qm",
        "feat: advance value",
        "-m",
        "Deliver the resumed change.",
    )
    delivered = git(world.repo, "rev-parse", "HEAD")
    world.manifest.write_text(
        f"node: fixture\nstatus: complete\ncommits: [{delivered}]\n"
    )
    _sweep()
    assert world.heads == [delivered]
