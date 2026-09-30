"""A review waits for the lane its own published document reports as saturated.

The reflex selects the lane it composes an automatic review onto — the owning
run's backend first, then the local one — and it reads a lane's own published
occupancy before composing. A serving lane publishes that document and a
backend declares it as ``lane_document``. Measured on 2026-09-30: the local lane
sampled at ``running 30`` against a computed ceiling of ``concurrent_requests
22`` with ``waiting 0``, so nothing was queueing and the pool was serving well
over its own stated limit; work composed onto it degraded rather than queuing.

Each assertion is argued inside out. A saturated local lane must withhold the
review rather than hand it to a metered lane, because a review is
local-lane-shaped work. A lane whose reading is healthy must keep its place, and
so must a lane whose reading is absent, stale, or the drained shape a lane
publishes when its pool empties — the hold rests on a measurement the document
made, and a missing reading must never hold a review.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
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

# The lane's own publication declares a shelf life, and the reflex additionally
# refuses a reading older than its own freshness bound. The fixture writes both
# values rather than importing the constant, so a change to either is a reading
# the test states rather than one it inherits.
LANE_SHELF_LIFE_SECONDS = 45


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


def _lane_config(lane_document: str | None) -> dict:
    """The fixture fleet, with the local lane declaring a document or not."""
    local = _backend("codex")
    if lane_document is not None:
        local["lane_document"] = lane_document
    return {
        "default_backend": LOCAL_BACKEND,
        "local_backend": LOCAL_BACKEND,
        "backends": {LOCAL_BACKEND: local, OTHER_BACKEND: _backend("claude")},
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _stamp(*, seconds_ago: float = 0.0) -> str:
    moment = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_lane_document(path: Path, payload: dict) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _saturated_document(tmp_path: Path) -> str:
    """The reading that motivated the hold: 30 running against a ceiling of 22."""
    return _write_lane_document(
        tmp_path / "lane-saturated.json",
        {
            "state": "measured",
            "running": 30,
            "concurrent_requests": 22,
            "concurrent_requests_instant": 22,
            "waiting": 0,
            "headroom": -8,
            "observed_at": _stamp(),
            "suggested_shelf_life_seconds": LANE_SHELF_LIFE_SECONDS,
        },
    )


def _drained_document(tmp_path: Path) -> str:
    """The shape a lane publishes once its pool empties and it stops sizing.

    Nothing is resident, so it declares no ceiling at all: the headroom is
    null, the verdict is ``do-not-size``, and only the instant key remains.
    That is a lane with room and no measurement over it, not a saturated one.
    """
    return _write_lane_document(
        tmp_path / "lane-drained.json",
        {
            "state": "measured",
            "running": 0,
            "waiting": 0,
            "headroom": None,
            "concurrent_requests_instant": None,
            "sizing_verdict": "do-not-size",
            "observed_at": _stamp(),
            "suggested_shelf_life_seconds": LANE_SHELF_LIFE_SECONDS,
        },
    )


def _healthy_document(tmp_path: Path) -> str:
    return _write_lane_document(
        tmp_path / "lane-healthy.json",
        {
            "state": "measured",
            "running": 5,
            "concurrent_requests": 22,
            "waiting": 0,
            "headroom": 17,
            "observed_at": _stamp(),
            "suggested_shelf_life_seconds": LANE_SHELF_LIFE_SECONDS,
        },
    )


def _stale_document(tmp_path: Path) -> str:
    """A reading older than the shelf life it states, and older than the bound."""
    return _write_lane_document(
        tmp_path / "lane-stale.json",
        {
            "state": "measured",
            "running": 30,
            "concurrent_requests": 22,
            "waiting": 0,
            "observed_at": _stamp(seconds_ago=600),
            "suggested_shelf_life_seconds": LANE_SHELF_LIFE_SECONDS,
        },
    )


def _candidates(lane_document: str | None) -> list[str]:
    return recovery._review_lane_candidates(_lane_config(lane_document))


def test_a_healthy_lane_keeps_its_place(tmp_path: Path) -> None:
    """The positive half: reading the document must not become a default hold."""
    assert _candidates(_healthy_document(tmp_path)) == [LOCAL_BACKEND, OTHER_BACKEND]


def test_a_drained_lane_reads_as_open(tmp_path: Path) -> None:
    """Running zero with no ceiling published is a lane with room, not one over."""
    assert _candidates(_drained_document(tmp_path)) == [LOCAL_BACKEND, OTHER_BACKEND]


def test_a_stale_reading_leaves_the_ordering_unchanged(tmp_path: Path) -> None:
    """An old figure describes a fleet that has since moved, so it holds nothing."""
    assert _candidates(_stale_document(tmp_path)) == [LOCAL_BACKEND, OTHER_BACKEND]


def test_an_absent_document_leaves_the_ordering_unchanged(tmp_path: Path) -> None:
    """A lane that publishes nothing is composed onto as before."""
    assert _candidates(None) == [LOCAL_BACKEND, OTHER_BACKEND]
    assert _candidates(str(tmp_path / "not-written.json")) == [
        LOCAL_BACKEND,
        OTHER_BACKEND,
    ]


def test_a_saturated_metered_lane_is_passed_over(tmp_path: Path) -> None:
    """A saturated lane is skipped; only the local lane withholds the selection."""
    config = _lane_config(None)
    config["backends"][OTHER_BACKEND]["lane_document"] = _saturated_document(tmp_path)
    assert recovery._review_lane_candidates(config, owning_backend=OTHER_BACKEND) == [
        LOCAL_BACKEND
    ]


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
        '<h2 id="s2">A review waits for a saturated lane</h2>',
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


def _scoring_pointer(config_home: Path, repo: Path, run_id: str) -> dict:
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
        "backend": LOCAL_BACKEND,
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _review_runs() -> list[dict]:
    return [
        row
        for row in runs.list_live(project="sample")
        if row["node"].get("id", "").startswith(recovery.REVIEW_NODE_PREFIX)
    ]


def test_a_saturated_local_lane_holds_the_review(
    tmp_path: Path, isolated_project: tuple[Path, Path]
) -> None:
    """The held case: no review run, and a reason that names the figures it read.

    The hold is not a refusal of the configuration — the fleet has a spawnable
    metered lane — so a reflex that fell back to it would dispatch a review here
    and this assertion would see a live review run and a composed backend.
    """
    config_home, repo = isolated_project
    document = _saturated_document(tmp_path)
    record = _scoring_pointer(config_home, repo, "r-saturated")

    report = recovery.dispatch_review_for_run(
        record,
        config=_lane_config(document),
        launcher=lambda *a, **k: os.getpid(),
    )

    assert report["dispatched"] is False
    assert report["awaiting_lane"] is True
    assert LOCAL_BACKEND in report["reason"]
    assert "30" in report["reason"] and "22" in report["reason"]
    assert OTHER_BACKEND not in report.get("backend", "")
    assert _review_runs() == []
    stored = runs.read_pointer("r-saturated")[recovery.REVIEW_DISPATCH_FIELD]
    assert stored["status"] == "awaiting-lane"
    assert LOCAL_BACKEND in stored["reason"]
    assert "30" in stored["reason"] and "22" in stored["reason"]


def test_a_healthy_local_lane_still_carries_the_review(
    tmp_path: Path, isolated_project: tuple[Path, Path]
) -> None:
    """The other positive half: a healthy local lane still leads a real compose."""
    config_home, repo = isolated_project
    document = _healthy_document(tmp_path)
    record = _scoring_pointer(config_home, repo, "r-healthy")
    try:
        with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
            report = recovery.dispatch_review_for_run(
                record,
                config=_lane_config(document),
                launcher=lambda *a, **k: os.getpid(),
            )
        assert report["dispatched"] is True
        assert report["backend"] == LOCAL_BACKEND
        assert runs.read_pointer(report["review_run_id"])["backend"] == LOCAL_BACKEND
    finally:
        _release_watcher()
