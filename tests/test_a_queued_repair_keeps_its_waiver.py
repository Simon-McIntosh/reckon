"""Reproduction: the repair reflex never dispatches a node, so nothing queues.

The plan asks that a repair queued while the local lane holds keep the launch
inputs the admission pass rebuilds from ``dispatch_options`` — the unreconciled
waiver and the reviewed-head base. That presumes the reflex can dispatch a
repair node that dispatch would queue. This file records the measurement that it
cannot, so the section's premise is visible rather than assumed.

``dispatch_repair_for_run`` reaches its composed ``dispatch()`` call only when
``_repair_launch_refusal(run_id, project)`` is truthy at the resume/dispatch
split — that is what skips the in-place resume block. The same predicate is then
re-evaluated immediately before the launch and refuses. So:

* an unpromoted reviewed run whose worker has exited takes the resume path (the
  composed findings ride a resume of the reviewed run itself), and
* a promoted reviewed run skips the resume path but is refused by the launch
  re-read ("the reviewed run is promoted").

No input reaches ``dispatch()``, the request is never queued, and the defect the
section names is latent. Both arms are asserted here against a recording stub,
so the finding is a fact about the code rather than a reading of it.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import copy
import importlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixtures
    CONFIG,
    PROJECT,
    SESSION,
    _lane_document,
    _launcher,
    admit_repo,
)

GRACE = "1s"
REVIEWED_RUN = "r-delivered-awaiting-repair"

FINDINGS = [
    {
        "file": "reckon/crew/thing.py",
        "line": "10",
        "text": "off-by-one in the loop",
        "severity": "blocking",
    },
]


def _config(tmp_path: Path, *, worker_slots: float) -> dict:
    config = copy.deepcopy(CONFIG)
    config.setdefault("fences", {})["unreconciled_run_grace"] = GRACE
    config.setdefault("roles", {})["review"] = {}
    config.setdefault("roles", {})["implement"] = {}
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=worker_slots)
    )
    return config


def _delivered_reviewed_run(repo: Path, head_sha: str) -> dict:
    """A completed implement run, older than the grace, carrying a finding review."""
    manifest = runs.crew_home() / "manifests" / f"{REVIEWED_RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: source-{REVIEWED_RUN}\nstatus: complete\ncommits: none\n",
        encoding="utf-8",
    )
    old = time.time() - 3600
    os.utime(manifest, (old, old))
    record = {
        "run_id": REVIEWED_RUN,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "repo": str(repo),
        "worktree": str(repo),
        "backend": "clive",
        "launch": "cli",
        "role": "implement",
        "phase": "starting",
        "node": {"id": "source-node", "plan": "plan-a", "section": "dispatch"},
        "manifest_path": str(manifest),
    }
    runs._write_json(runs.pointer_path(REVIEWED_RUN), record)
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": REVIEWED_RUN,
            "reviewed_base_sha": head_sha,
            "reviewed_head_sha": head_sha,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
            "total": 75,
            "findings": list(FINDINGS),
        }
    )
    return record


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_an_unpromoted_repair_resumes_in_place_and_never_dispatches(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unpromoted arm: the composed findings ride a resume, not a dispatch.

    A recording stub on ``reckon.crew.dispatch.dispatch`` observes the call the
    reflex *would* make. It is called zero times, which is the finding: there is
    no queued request to carry a ``dispatch_options`` waiver.
    """
    record = _delivered_reviewed_run(admit_repo, _head(admit_repo))
    config = _config(tmp_path, worker_slots=0)
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-repair"}

    monkeypatch.setattr(dispatch_module, "dispatch", spy)
    report = recovery.dispatch_repair_for_run(
        record, config=config, launcher=_launcher([])
    )

    assert calls == []
    assert report["dispatched"] is False
    # The resume path was taken: the report carries no dispatch and no queued
    # request, and no repair pointer was written.
    assert "queued" not in report
    assert [p["run_id"] for p in runs.list_live(project=PROJECT, phase="queued")] == []


def test_a_promoted_repair_is_refused_and_never_dispatches(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promoted arm: the launch re-read refuses the node the split routed here."""
    from reckon import ledger

    record = _delivered_reviewed_run(admit_repo, _head(admit_repo))
    run_file = ledger.run_path(PROJECT, REVIEWED_RUN)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    runs._write_json(run_file, {"run_id": REVIEWED_RUN, "status": "promoted"})
    config = _config(tmp_path, worker_slots=0)
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-repair"}

    monkeypatch.setattr(dispatch_module, "dispatch", spy)
    report = recovery.dispatch_repair_for_run(
        record, config=config, launcher=_launcher([])
    )

    assert calls == []
    assert report["dispatched"] is False
    assert report.get("refused") is True
    assert report["reason"] == "the reviewed run is promoted"
