"""A queued review keeps the unreconciled-runs waiver across its relaunch.

A review holds the waiver because a run awaiting review *is* an unreconciled
run: past the grace the fence refuses the very dispatch that reconciles it. The
waiver reaches dispatch as ``unreconciled_override``; it is recorded on a queued
request only through ``dispatch_options``, and the admission pass rebuilds it
from there when the lane admits the request. A caller that passes the override
without the option therefore queues a request that fails ``UnreconciledRuns`` on
every admission pass — the deadlock. These cases drive the real reflex and
composed-review dispatch paths, hold the lane, admit the queued request, and
find the review launches while an implementation node dispatched in the same
state is still refused. A stranded review's discard reason is read from wherever
the discard recorded it, including the spool a never-launched request uses.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import copy
import importlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import (
    plan_review,
    queue_admission,
    recovery,
    recovery_review_dispatch,
    runs,
)
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    CONFIG,
    PROJECT,
    SESSION,
    _lane_document,
    _launcher,
    admit_repo,
)

GRACE = "1s"
DELIVERED_RUN = "r-delivered-awaiting-review"


def _config(tmp_path: Path, *, worker_slots: float) -> dict:
    config = copy.deepcopy(CONFIG)
    config.setdefault("fences", {})["unreconciled_run_grace"] = GRACE
    config.setdefault("roles", {})["review"] = {}
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=worker_slots)
    )
    return config


def _resolve(config: dict):
    def resolve(pointer):
        return config

    return resolve


def _delivered_scoring_run(repo: Path, *, run_id: str = DELIVERED_RUN) -> dict:
    """A completed run awaiting review, older than the grace, with no review."""
    manifest = runs.crew_home() / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: source-{run_id}\nstatus: complete\ncommits: none\n", encoding="utf-8"
    )
    old = time.time() - 3600
    os.utime(manifest, (old, old))
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "repo": str(repo),
        "worktree": str(repo),
        "backend": "clive",
        "launch": "cli",
        "phase": "starting",
        "node": {"id": f"source-{run_id}", "plan": "plan-a", "section": "dispatch"},
        "manifest_path": str(manifest),
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _queued_options(result: dict) -> dict:
    queued = result["queued"]
    return runs.read_pointer(str(queued["run_id"]))["dispatch_options"]


def test_a_queued_reflex_review_carries_the_waiver_and_launches(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The reflex queues a review while the lane holds; the relaunch carries the waiver."""
    record = _delivered_scoring_run(admit_repo)
    config = _config(tmp_path, worker_slots=0)

    report = recovery_review_dispatch.dispatch_review_for_run(
        record, config=config, launcher=_launcher([])
    )

    assert "queued" in report, report
    options = _queued_options(report)

    # Open the lane and admit: the review launches rather than being refused
    # for the run whose review it is.
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=1)
    )
    admitted = queue_admission.admit_once(
        config_for=_resolve(config), launcher=_launcher([])
    )

    assert admitted["launched"], admitted.get("failure_detail", admitted)
    assert not admitted["failed"], admitted
    assert runs.read_pointer(admitted["launched"][0])["phase"] != "queued"
    assert options["allow_unreconciled_runs"] is True


def test_an_implementation_dispatch_in_the_same_state_is_still_refused(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The control: without the waiver, the same fence refuses the relaunch."""
    _delivered_scoring_run(admit_repo)
    config = _config(tmp_path, worker_slots=1)
    from tests.test_a_queued_run_is_admitted import _queue

    runs.queue_dispatch(_queue(admit_repo, node_id="impl-node", run_id="r-impl"))

    admitted = queue_admission.admit_once(
        config_for=_resolve(config), launcher=_launcher([])
    )

    assert not admitted["launched"], admitted
    assert admitted["failed"], admitted
    detail = " ".join(str(value) for value in admitted["failure_detail"].values())
    assert "unreconciled run(s) older than" in detail, detail


def test_a_composed_review_dispatch_records_the_waiver(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composed-review path records the same option on its dispatch call."""
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    captured: dict = {}

    def spy(**kwargs):
        captured.update(kwargs)
        return {"run_id": "r-composed-review", "phase": "running"}

    fields = {
        "node_id": "review-composed",
        "goal": "review the portfolio",
        "brief": "",
        "done_when": "the review stores its record",
        "write_paths": ["reports/portfolio"],
        "time_budget": "20m",
        "session": SESSION,
        "project": PROJECT,
        "brief_text": "review the portfolio",
    }
    monkeypatch.setattr(dispatch_module, "dispatch", spy)
    monkeypatch.setattr(
        plan_review, "portfolio_review_fields", lambda subject, write=True: fields
    )

    plan_review.dispatch_portfolio_review(
        {"project": PROJECT, "local": True},
        config=_config(tmp_path, worker_slots=1),
    )

    assert captured["dispatch_options"]["allow_unreconciled_runs"] is True


REVIEW_RUN = "r-strand-review"
SOURCE_RUN = "r-strand-source"
NOW = 1_800_000_000.0


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _scoring_pointer(review_run_id: str) -> dict:
    manifest = runs.crew_home() / "manifests" / f"{SOURCE_RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {SOURCE_RUN}\nstatus: complete\ncommits: none\n")
    record = {
        "run_id": SOURCE_RUN,
        "project": PROJECT,
        "session": SESSION,
        "phase": "starting",
        "process_alive": False,
        "manifest_path": str(manifest),
        "node": {
            "id": "source-node",
            "plan": "fixture-plan",
            "section": "s",
            "write_paths": ["seed.txt"],
        },
        recovery.REVIEW_DISPATCH_FIELD: {
            "run_id": review_run_id,
            "status": "dispatched",
            "head": "",
        },
    }
    runs._write_json(runs.pointer_path(SOURCE_RUN), record)
    return record


def _stranded_review_pointer(review_run_id: str) -> dict:
    """A stranded review pointer with no worker evidence and no run directory."""
    started = _at(time.time() - 2 * recovery.STRANDED_LAUNCH_BOUND_SECONDS)
    record = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "starting",
        "created_at": started,
        "attempt_started_at": started,
    }
    runs._write_json(runs.pointer_path(review_run_id), record)
    return record


def test_a_never_launched_stranded_review_keeps_its_strand_reason(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review with no run directory records its reason in the discard spool."""
    _scoring_pointer(REVIEW_RUN)
    _stranded_review_pointer(REVIEW_RUN)
    assert not runs.run_dir(REVIEW_RUN).exists()

    monkeypatch.setattr(
        recovery_review_dispatch, "carry_review_forward", lambda *a, **k: None
    )

    report = recovery.dispatch_review_for_run(runs.read_pointer(SOURCE_RUN))

    assert report["reason"] != "a review is already in flight as a live run"
    assert not runs.pointer_path(REVIEW_RUN).exists()
    assert not runs.run_dir(REVIEW_RUN).exists()
    from reckon.crew import promotion

    spool = promotion.discard_spool_path(REVIEW_RUN)
    assert spool.is_file()
    marker = json.loads(spool.read_text(encoding="utf-8"))
    assert marker["run_id"] == REVIEW_RUN
    assert "stranded launch" in marker["reason"]
