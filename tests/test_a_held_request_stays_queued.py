"""A held request is retried without being withdrawn and re-queued.

The admission pass retries every queued request on its cadence. Releasing a
pointer before asking the lane whether it would admit the launch makes each
retry emit a new run identity for a request whose state never moved: the
release withdraws the old run and the refused launch queues a fresh id, so the
pane draws ``queued -> dropped`` and ``dropped -> queued`` for a request that
was never anything but queued. These tests bind the pre-release questions -- the
lane's gate document and the session's worker allowance -- so a request the lane
would hold is left exactly as it is, while a request the lane will admit still
launches.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import json
import os
from pathlib import Path

from reckon.crew import queue_admission, recovery, runs
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    _config_for,
    _lane_document,
    _queue,
    admit_repo,
)

PROJECT = "proj"
_MOMENT = 1_800_000.0


def _paused_gate(tmp_path: Path) -> Path:
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"paused": True, "reason": "moving"}), encoding="utf-8")
    return gate


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _fleet(project: str) -> dict:
    """Every live pointer of the project, keyed by run id.

    ``_list_live_records`` reads the pointers without publishing a watcher
    transition of its own, so the fold below is the only thing that turns a
    fleet observation into a stream row.
    """
    return {p["run_id"]: p for p in runs._list_live_records(project=project)}


def _stream_rows(before: dict, after: dict) -> list:
    """The transitions the project's watch stream would draw between two reads."""
    known = {
        run_id: recovery._watch_snapshot(pointer, moment=_MOMENT, stall_seconds=3600)
        for run_id, pointer in before.items()
    }
    current = {
        run_id: recovery._watch_snapshot(
            pointer, moment=_MOMENT + 1, stall_seconds=3600
        )
        for run_id, pointer in after.items()
    }
    events, _ = recovery.fleet_transitions(known, current, ledger_run_ids=dict)
    return events


def test_a_request_the_lane_gate_holds_is_left_untouched(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A paused lane gate holds the request in place.

    The gate is the first question the dispatch path asks, so a request it holds
    must not be released at all: releasing it withdraws the run and the refused
    launch writes it back carrying a failure, which is a state change for a
    request nothing moved.
    """
    config, resolve = _config_for()
    config["backends"]["clive"]["gate_document"] = str(_paused_gate(tmp_path))
    runs.queue_dispatch(_queue(admit_repo, node_id="held", run_id="r-held"))
    before = runs.pointer_path("r-held").read_bytes()
    fleet_before = _fleet(PROJECT)

    result = queue_admission.admit_once(config_for=resolve)

    assert result["launched"] == []
    assert "r-held" in result["held"]
    assert runs.pointer_path("r-held").read_bytes() == before
    assert _stream_rows(fleet_before, _fleet(PROJECT)) == []


def test_a_request_the_lane_allowance_holds_is_left_untouched(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A session the router grants no room holds its request in place."""
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=0)
    )
    runs.queue_dispatch(_queue(admit_repo, node_id="held", run_id="r-held"))
    before = runs.pointer_path("r-held").read_bytes()
    fleet_before = _fleet(PROJECT)

    result = queue_admission.admit_once(config_for=resolve)

    assert result["launched"] == []
    assert "r-held" in result["held"]
    assert runs.pointer_path("r-held").read_bytes() == before
    assert _stream_rows(fleet_before, _fleet(PROJECT)) == []


def test_the_same_request_launches_once_the_lane_opens(
    admit_repo: Path, tmp_path: Path
) -> None:
    """With the lane open, the same request is released and launched."""
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=1)
    )
    runs.queue_dispatch(_queue(admit_repo, node_id="ready", run_id="r-ready"))
    recorded: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(recorded)
    )

    assert len(result["launched"]) == 1
    assert recorded
    assert runs.read_pointer(result["launched"][0])["phase"] != "queued"
