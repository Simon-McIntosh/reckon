"""A composed review is never placed on a backend the calling harness owns.

A backend declared with an in-harness launch is started by a coordinator that
attaches it to a task it is already running, so reckon cannot spawn one. A
review the reflex composes onto it is recorded as dispatched and never
executes, while it still holds the review's write-path claim — a run that
fails by doing nothing, which is the failure this file exists to keep out.
The exclusion is read from the backend declaration rather than from a list of
names, because a list is maintained by hand in every project layer and one
that omits the name gets the unlaunchable review back.

Both properties are argued with the discriminating negative: restore an
exclusion that only removes names the configuration already lists, and an
in-harness backend is back in the candidate set and composed onto.
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


# The lane the owning run recorded, the locally served lane, a third lane a
# fallback can land on, and the calling harness itself.
OWNING_BACKEND = "clive"
LOCAL_BACKEND = "alpha"
OTHER_BACKEND = "beta"
HARNESS_BACKEND = "native"


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


def _in_harness_backend() -> dict:
    return {
        "launch": "in-harness",
        "sandbox": "worktree-full",
        "session_reuse": False,
    }


# A fleet shaped like the one the fall-through was measured on: the locally
# served lane and every other spawnable lane withheld by configuration, and the
# in-harness backend the one candidate an alphabetical fallback reaches.
CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend(LOCAL_BACKEND, "codex"),
        OTHER_BACKEND: _backend(OTHER_BACKEND, "claude"),
        OWNING_BACKEND: _backend(OWNING_BACKEND, "claude"),
        HARNESS_BACKEND: _in_harness_backend(),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    recovery.REVIEW_EXCLUDED_BACKENDS_KEY: [LOCAL_BACKEND, OTHER_BACKEND],
}

# The same fleet with no exclusion at all, so the in-harness backend is the
# only thing this ordering can be dropping it for.
CONFIG_WITHOUT_EXCLUSIONS = {
    **CONFIG,
    recovery.REVIEW_EXCLUDED_BACKENDS_KEY: [],
}

# Only lanes the calling harness owns, which is the shape that leaves a review
# with no lane rather than with a lane nothing starts.
CONFIG_ONLY_IN_HARNESS = {
    "default_backend": HARNESS_BACKEND,
    "local_backend": HARNESS_BACKEND,
    "backends": {HARNESS_BACKEND: _in_harness_backend()},
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
    backend: str = OWNING_BACKEND,
    session: str = "session-orchestrating",
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
        "backend": backend,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": session,
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _dispatch_review(record: dict, config: dict) -> dict:
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        return recovery.dispatch_review_for_run(
            record, config=config, launcher=lambda *a, **k: os.getpid()
        )


def test_an_in_harness_backend_is_not_a_review_lane_candidate() -> None:
    """The declared launch kind removes it, with the exclusion key silent.

    The backend is not named in ``review_excluded_backends`` anywhere in this
    fixture, and it is the owning run's recorded backend in one of the two
    calls, so a rule that reads names rather than declarations reports it here.
    """
    assert HARNESS_BACKEND not in recovery._review_excluded_backends(CONFIG)
    candidates = recovery._review_lane_candidates(CONFIG)
    assert HARNESS_BACKEND not in candidates
    assert candidates, "the fleet still has a spawnable lane"
    owning = recovery._review_lane_candidates(CONFIG, owning_backend=HARNESS_BACKEND)
    assert HARNESS_BACKEND not in owning
    assert owning == candidates


def test_a_review_whose_only_lane_is_in_harness_is_held_with_a_reason(
    isolated_project: tuple[Path, Path],
) -> None:
    """No lane the harness cannot start leaves the run held, not composed.

    The hold is the same one an empty candidate list produces, so the run keeps
    its reason on its own pointer and no review run exists to hold a claim.
    """
    config_home, repo = isolated_project
    record = _scoring_pointer(
        config_home, repo, "r-single-lane", backend=HARNESS_BACKEND
    )
    try:
        report = _dispatch_review(record, CONFIG_ONLY_IN_HARNESS)
        assert report["dispatched"] is False
        assert report["awaiting_lane"] is True
        assert "r-single-lane" in report["reason"]
        assert not any(
            row["node"].get("id", "").startswith(recovery.REVIEW_NODE_PREFIX)
            for row in runs.list_live(project="sample")
        )
    finally:
        _release_watcher()


def test_a_review_never_lands_on_the_owning_runs_in_harness_backend(
    isolated_project: tuple[Path, Path],
) -> None:
    """The owning run's own lane is dropped when the harness owns it.

    The run recorded the in-harness backend, which a fallback reading names
    would lead with; the review of it is composed onto the next spawnable lane
    instead, and the pointer records a launch reckon can start.
    """
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-owned", backend=HARNESS_BACKEND)
    try:
        report = _dispatch_review(record, CONFIG)
        assert report["dispatched"] is True
        assert report["backend"] != HARNESS_BACKEND
        landed = runs.read_pointer(report["review_run_id"])
        assert landed["backend"] != HARNESS_BACKEND
        assert landed["launch"] != recovery.IN_HARNESS_LAUNCH
    finally:
        _release_watcher()


def test_a_spawnable_lane_is_still_composed_when_one_exists(
    isolated_project: tuple[Path, Path],
) -> None:
    """The drop is a filter, not a refusal: a spawnable lane still dispatches.

    Without this arm the other three would pass on an ordering that answered
    every configuration with an empty list.
    """
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-ordinary", backend=OWNING_BACKEND)
    try:
        report = _dispatch_review(record, CONFIG_WITHOUT_EXCLUSIONS)
        assert report["dispatched"] is True
        assert report["backend"] == OWNING_BACKEND
        landed = runs.read_pointer(report["review_run_id"])
        assert landed["backend"] == OWNING_BACKEND
        assert landed["launch"] == "cli"
    finally:
        _release_watcher()
