"""A composed review follows the lane its owning run recorded, not the sweeper's.

A review is a second dispatch of a run's work, and the lane it lands on belongs
to the coordinator that chose the lane the original run carried rather than to
whichever session happens to be sweeping. A sweep composing every review onto
its own locally served backend attributes a lane its owner never chose, and it
also steps over a backend the flight configuration has removed from review
routing, because a fallback walking a list keeps walking until it finds one.

Each property is argued with its discriminating negative, because the failure
here is silent: a review dispatched onto the wrong lane still produces a review,
just not the one the configuration asked for. The exclusion is asserted against
each axis — no composed lane, whichever fallback walks, and the printed command
a reader may retype.

The owning lane deliberately differs from the locally served backend: a
composition that ignored the owning run's recorded backend would still land on
some configured lane, and only a fixture whose local lane is not the owning lane
can tell a selection from an assertion.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from reckon import crew
from reckon.crew import recovery, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver, which would let
# every dispatch through and prove nothing about the refusal.
pytestmark = pytest.mark.arms_watch_producer


# The lane the owning run recorded, the locally served lane this session would
# otherwise compose onto, and a third lane so a fallback has somewhere to land
# after the excluded one.
OWNING_BACKEND = "clive"
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
        OWNING_BACKEND: _backend(OWNING_BACKEND, "claude"),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _config(*excluded: str) -> dict:
    """The fleet, optionally with a set of backends removed from review routing."""
    if not excluded:
        return dict(CONFIG)
    return {**CONFIG, recovery.REVIEW_EXCLUDED_BACKENDS_KEY: list(excluded)}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-ship" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
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
    backend: str = LOCAL_BACKEND,
    session: str = "session-orchestrating",
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
        "backend": backend,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": session,
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


def _compose(record: dict, config: dict | None = None) -> dict:
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        return recovery.dispatch_review_for_run(
            record, config=config or CONFIG, launcher=lambda *a, **k: os.getpid()
        )


def test_a_review_runs_on_the_lane_its_owning_run_recorded(
    isolated_project: tuple[Path, Path],
) -> None:
    """The owning lane wins even where it is not the locally served lane."""
    config_home, repo = isolated_project
    record = _scoring_pointer(config_home, repo, "r-own", backend=OWNING_BACKEND)
    try:
        report = _compose(record)
        assert report["dispatched"] is True
        assert report["backend"] and report["backend"] == OWNING_BACKEND
        landed = runs.read_pointer(report["review_run_id"])
        assert landed["backend"] == OWNING_BACKEND
    finally:
        _release_watcher()


def test_a_failed_owning_lane_falls_back_only_to_a_lane_not_excluded(
    isolated_project: tuple[Path, Path],
) -> None:
    """The lane that dropped it is skipped, and so is every excluded lane.

    The locally served lane is the next candidate here and it is the excluded
    one, so a fallback that ignores the exclusion key lands on it.
    """
    config_home, repo = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-dropped",
        backend=OWNING_BACKEND,
        previous=_failed_attempt(OWNING_BACKEND),
    )
    try:
        report = _compose(record, _config(LOCAL_BACKEND))
        assert report["dispatched"] is True
        assert report["backend"] == OTHER_BACKEND
        assert report["backend"] != LOCAL_BACKEND
        landed = runs.read_pointer(report["review_run_id"])
        assert landed["backend"] == OTHER_BACKEND
        recorded = runs.read_pointer("r-dropped")[recovery.REVIEW_DISPATCH_FIELD]
        assert recorded["backend"] == OTHER_BACKEND
    finally:
        _release_watcher()


def test_a_fully_excluded_wave_refuses_naming_the_exclusion(
    isolated_project: tuple[Path, Path],
) -> None:
    """Every remaining candidate excluded: the review is refused, not sent.

    The refusal names the exclusion, because a reader told only that no
    configured backend remains would look for a lane to add, when the
    configuration withholds the lane deliberately.
    """
    config_home, repo = isolated_project
    record = _scoring_pointer(
        config_home,
        repo,
        "r-barred",
        backend=OWNING_BACKEND,
        previous=_failed_attempt(OWNING_BACKEND),
    )
    try:
        report = _compose(record, _config(LOCAL_BACKEND, OTHER_BACKEND))
        assert report["dispatched"] is False
        assert report["awaiting_lane"] is True
        assert recovery.REVIEW_EXCLUDED_BACKENDS_KEY in report["reason"]
        assert not any(
            row["node"].get("id", "").startswith(recovery.REVIEW_NODE_PREFIX)
            for row in runs.list_live(project="sample")
        )
    finally:
        _release_watcher()


def test_the_sweep_bookends_only_its_own_sessions_runs(
    isolated_project: tuple[Path, Path],
) -> None:
    """A sweep composes only the reviews its own session owns.

    Another session's run is that session's to compose, because only that
    coordinator chose its lane and would spend its member.
    """
    config_home, repo = isolated_project
    _scoring_pointer(config_home, repo, "r-mine", backend=OWNING_BACKEND)
    _scoring_pointer(
        config_home,
        repo,
        "r-theirs",
        backend=OWNING_BACKEND,
        session="session-someone-else",
    )
    try:
        with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
            recovery.dispatch_awaiting_reviews(
                project="sample",
                config=CONFIG,
                launcher=lambda *a, **k: os.getpid(),
            )
        assert recovery.REVIEW_DISPATCH_FIELD in runs.read_pointer("r-mine")
        assert recovery.REVIEW_DISPATCH_FIELD not in runs.read_pointer("r-theirs")
    finally:
        _release_watcher()


def test_the_copyable_next_action_names_the_owning_runs_lane(
    isolated_project: tuple[Path, Path],
) -> None:
    """The command a reader retypes carries the owning lane, not a local one."""
    config_home, repo = isolated_project
    _scoring_pointer(config_home, repo, "r-print", backend=OWNING_BACKEND)
    record = runs.read_pointer("r-print")
    argv = recovery._review_dispatch_argv(record)
    assert argv[argv.index("--backend") + 1] == OWNING_BACKEND
    assert "--local" not in argv
    action = recovery._review_dispatch_action(record)
    assert "--backend " + OWNING_BACKEND in action
    assert "--local" not in action


def test_the_flight_artifacts_agree_on_the_exclusion_key() -> None:
    """The source, the generated model and the generated schema all declare it."""
    key = recovery.REVIEW_EXCLUDED_BACKENDS_KEY
    root = Path(__file__).parents[1]
    source = yaml.safe_load(
        (root / "reckon" / "schema" / "flight.yaml").read_text(encoding="utf-8")
    )
    assert key in source["slots"]
    assert key in source["classes"]["FlightConfig"]["slots"]

    from reckon._flight_schema import FlightConfig

    assert key in FlightConfig.model_fields

    schema = json.loads(
        (root / "docs" / "_shared" / "flight.schema.json").read_text(encoding="utf-8")
    )
    assert key in schema["properties"]
