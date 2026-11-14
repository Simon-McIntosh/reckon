"""A finished review run releases the record path a later review claims.

A run review declares the reviewed run's ``review-records/<run>.json`` as its
own record path, so a follow-up review of the same run names the same path.
When the later review is dispatched while an earlier review run has delivered
its record but has not been promoted, that earlier run's claim still fences the
path — its recorded worktree is gone, so the claim reads as work nobody can
inspect — and the dispatch is refused ``scope-conflict``.

A review's whole deliverable is the record it stores, so once that record
parses and its own manifest reads complete the run has nothing left to write
and its claim must not fence a later review of the same run. A reviewer that
has not delivered keeps its claim, and the refusal in that case is the one the
claim exists for.

Every store and crew home here resolves under the test's temporary config home;
the operator's own pointer directory is asserted untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex dispatch is gated by the watch admission, so these tests arm the
# producer the gate reads rather than accepting the suite-wide waiver.
pytestmark = pytest.mark.arms_watch_producer

PROJECT = "release-claim-fixture"
SESSION = "session-orchestrating"
REVIEWED_RUN = "r-20261012T120000000001-reviewed-run"
REVIEW_RUN = "r-20261012T130000000000-review-of-reviewed-run"
REVIEWED_BASE = "b" * 40
REVIEWED_HEAD = "a" * 40

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


def _real_pointer() -> Path:
    return Path.home() / ".config" / "reckon" / "crew" / "live" / f"{REVIEW_RUN}.json"


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_target() -> None:
    """No fixture may reach the operator's live pointer directory."""
    assert not _real_pointer().exists()
    yield
    assert not _real_pointer().exists()


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def isolated_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "repo"
    scripts = repository / "skills" / "reckon-build" / "scripts"
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
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">A finished review releases its claim</h2>',
        encoding="utf-8",
    )
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt", "docs"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )

    dispatch_module = __import__("reckon.crew.dispatch", fromlist=["dispatch"])

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repository


def _manifest(config_home: Path, run_id: str, *, complete: bool) -> Path:
    path = config_home / "manifests" / f"{run_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = "complete" if complete else "in-progress"
    path.write_text(
        f"node: {run_id}\nstatus: {status}\ncheckpoint: a checkpoint\n",
        encoding="utf-8",
    )
    return path


def _reviewed_pointer(config_home: Path, repository: Path) -> dict:
    """The run awaiting review, cut from REVIEWED_BASE and reading REVIEWED_HEAD."""
    head = _git(repository, "rev-parse", "HEAD")
    record = {
        "run_id": REVIEWED_RUN,
        "project": PROJECT,
        "repo": str(repository),
        "node": {
            "id": "reviewed-node",
            "plan": "fixture",
            "section": "s2",
            "write_paths": ["seed.txt"],
        },
        "backend": LOCAL_BACKEND,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": SESSION,
        "manifest_path": str(_manifest(config_home, REVIEWED_RUN, complete=True)),
        "worktree": str(repository),
        "base_sha": REVIEWED_BASE,
        "head": head,
    }
    runs._write_json(runs.pointer_path(REVIEWED_RUN), record)
    return record


def _record_paths() -> tuple[Path, Path]:
    """The headless and head-keyed record paths a run review declares."""
    directory = review_module.run_review_record_directory(runs.run_dir(REVIEWED_RUN))
    return (
        directory / f"{REVIEWED_RUN}.json",
        directory / f"{REVIEWED_RUN}.at-{REVIEWED_HEAD}.json",
    )


def _store_record() -> Path:
    """Write a complete parsed review record and return the headless path."""
    headless, keyed = _record_paths()
    headless.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": REVIEWED_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "total": 20 * len(review_module.REVIEW_DIMENSIONS),
        "findings": [],
        "timestamp": "2026-10-12T13:00:00Z",
    }
    text = json.dumps(body)
    headless.write_text(text, encoding="utf-8")
    keyed.write_text(text, encoding="utf-8")
    return headless


def _dead_pid() -> int:
    for candidate in (999999, 999998, 999997, 999996):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
    raise AssertionError("no dead pid available")


def _review_pointer(
    config_home: Path,
    repository: Path,
    *,
    delivered: bool,
) -> dict:
    """A review run that has ended; delivered keeps every delivery receipt."""
    headless, keyed = _record_paths()
    node = {
        "id": f"review-of-{REVIEWED_RUN}",
        "plan": "fixture",
        "section": "s2",
        "time_budget": "25m",
        "role": "review",
        "write_paths": [str(headless), str(keyed)],
    }
    record = {
        "run_id": REVIEW_RUN,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "review",
        "backend": "native",
        "created_at": "2026-10-12T13:00:00Z",
        "manifest_path": str(_manifest(config_home, REVIEW_RUN, complete=delivered)),
        # The checkout a read-only review was handed is reclaimed when its
        # worker ends, which is the shape whose claim cannot be re-inspected.
        "worktree": str(repository.parent / "reclaimed-review-worktree"),
        "base_sha": REVIEWED_HEAD,
        "phase": "stopped",
        "pid": _dead_pid(),
        "node": node,
    }
    run_directory = runs.run_dir(REVIEW_RUN)
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN, "exit_code": 0}), encoding="utf-8"
    )
    runs._write_json(runs.pointer_path(REVIEW_RUN), record)
    return record


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


def _compose(record: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Dispatch this run's review through the reflex, as a sweep would."""
    monkeypatch.setattr(recovery, "carry_review_forward", lambda *args, **kwargs: None)
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        return recovery.dispatch_review_for_run(
            record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )


def test_a_finished_review_releases_its_record_path(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered review's claim no longer refuses the next review of the run."""
    config_home, repository = isolated_project
    record = _reviewed_pointer(config_home, repository)
    stored = _store_record()
    before = stored.read_bytes()
    _review_pointer(config_home, repository, delivered=True)
    try:
        report = _compose(record, monkeypatch)
        stored_after = _record_paths()[0].read_bytes()
    finally:
        _release_watcher()

    assert report["dispatched"] is True, report.get("reason")
    # The visible mark of the release: the finished review's pointer is gone.
    assert not runs.pointer_path(REVIEW_RUN).exists()
    assert report.get("released_reviews"), report
    assert report["released_reviews"][0]["run_id"] == REVIEW_RUN
    assert stored_after == before


def test_a_working_review_keeps_its_claim(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer that has not delivered still refuses the second dispatch."""
    config_home, repository = isolated_project
    record = _reviewed_pointer(config_home, repository)
    _review_pointer(config_home, repository, delivered=False)
    try:
        report = _compose(record, monkeypatch)
    finally:
        _release_watcher()

    assert report["dispatched"] is False, report.get("reason")
    assert report["refused"] is True
    recorded = runs.read_pointer(REVIEWED_RUN)[recovery.REVIEW_DISPATCH_FIELD]
    assert recorded["status"] == "refused"
    assert recorded["claim"]["run_id"] == REVIEW_RUN
    assert runs.pointer_path(REVIEW_RUN).exists()
