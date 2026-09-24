"""The reflex selects the lane it composes a review onto rather than asserting one.

The reflex used to name the locally served backend unconditionally, so every
automatic review landed there whatever that lane was doing — including on the
lane whose previous attempt had just been dropped. A sweep fires on every
completion, so an assertion rather than a selection recomposes the same review
onto the same lane, and each attempt spends a member to reach the same
condition.

Each assertion here is argued inside out. A first composition must still prefer
the local lane, because a fallback that quietly becomes the default is a
different defect with the same shape. Only then may a recorded failed attempt
demote that lane, and the recomposition must land on a lane the run has not
already been dropped by.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer


LOCAL_BACKEND = "alpha"
OTHER_BACKEND = "beta"


def _backend(name: str, command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend(LOCAL_BACKEND, "codex"),
        OTHER_BACKEND: _backend(OTHER_BACKEND, "claude"),
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
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A finished run dispatches its own review</h2>',
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
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
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
        if not crew.watch_state("sample")["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _release_watcher() -> None:
    if crew.watch_state("sample")["watcher_live"]:
        recovery.unwatch("sample")
        _wait_for_stopped_producer()


def _scoring_pointer(
    config_home: Path,
    repo: Path,
    run_id: str,
    *,
    previous: dict | None = None,
) -> dict:
    manifest = config_home / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\ncommits: " + run_id + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    if previous is not None:
        record[recovery.REVIEW_DISPATCH_FIELD] = previous
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _failed_attempt(backend: str) -> dict:
    """A review dispatch the run records, whose review run is no longer alive."""
    return {
        "status": "dispatched",
        "reason": f"the review dispatched automatically as run r-{backend}-dead",
        "run_id": None,
        "backend": backend,
        "at": "2026-09-22T10:00:00Z",
        "attempt": 1,
    }


def _compose(record: dict) -> dict:
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        return recovery.dispatch_review_for_run(
            record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )


def test_a_first_composition_prefers_the_local_lane(
    isolated_project: tuple[Path, Path],
) -> None:
    """The positive half: selection must not turn the fallback into the default."""
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-first")
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == LOCAL_BACKEND
        landed = runs.read_pointer(report["review_run_id"])
        assert landed["backend"] == LOCAL_BACKEND
    finally:
        _release_watcher()


def test_a_not_already_attempted_lane_is_not_reselected(
    isolated_project: tuple[Path, Path],
) -> None:
    """A lane that already dropped this review is not composed onto again."""
    config_home, repo = isolated_project
    record = _scoring_pointer(
        config_home, repo, "r-dropped", previous=_failed_attempt(LOCAL_BACKEND)
    )
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] == OTHER_BACKEND
        assert report["backend"] != LOCAL_BACKEND
        landed = runs.read_pointer(report["review_run_id"])
        assert landed["backend"] == OTHER_BACKEND
        recorded = runs.read_pointer("r-dropped")[recovery.REVIEW_DISPATCH_FIELD]
        assert recorded["backend"] == OTHER_BACKEND
    finally:
        _release_watcher()


def test_two_sweeps_do_not_recompose_onto_the_dropped_lane(
    isolated_project: tuple[Path, Path],
) -> None:
    """Driving the reflex twice advances the lane rather than retrying it."""
    config_home, repo = isolated_project
    _scoring_pointer(config_home, repo, "r-swept")
    first: dict = {}
    try:
        with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
            first = recovery.dispatch_review_for_run(
                runs.read_pointer("r-swept"),
                config=CONFIG,
                launcher=lambda *a, **k: os.getpid(),
            )
            assert first["dispatched"] is True
            assert first["backend"] == LOCAL_BACKEND
            # The dispatched review abandons inside its first minute: its run
            # pointer is gone, and the source run is left holding only the
            # record of the attempt.
            crew.pointer_path(first["review_run_id"]).unlink()
            second = recovery.dispatch_review_for_run(
                runs.read_pointer("r-swept"),
                config=CONFIG,
                launcher=lambda *a, **k: os.getpid(),
            )
        assert second["dispatched"] is True
        assert second["backend"] == OTHER_BACKEND
        assert second["backend"] != first["backend"]
    finally:
        _release_watcher()


def test_no_alternative_lane_refuses_rather_than_retrying_the_dropped_one(
    isolated_project: tuple[Path, Path],
) -> None:
    """With only the dropped lane configured the reflex states the refusal."""
    config_home, repo = isolated_project
    record = _scoring_pointer(
        config_home, repo, "r-stuck", previous=_failed_attempt(LOCAL_BACKEND)
    )
    solo = {
        **CONFIG,
        "backends": {LOCAL_BACKEND: CONFIG["backends"][LOCAL_BACKEND]},
    }
    report = recovery.dispatch_review_for_run(
        record, config=solo, launcher=lambda *a, **k: os.getpid()
    )
    assert report["dispatched"] is False
    assert report["awaiting_lane"] is True
    assert LOCAL_BACKEND in report["reason"]
    assert not any(
        row["node"].get("id", "").startswith(recovery.REVIEW_NODE_PREFIX)
        for row in runs.list_live(project="sample")
    )
