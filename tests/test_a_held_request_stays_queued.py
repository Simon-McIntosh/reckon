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


def _stream_file_rows(project: str, run_id: str) -> list:
    """The rows the project's watch stream carries for one run id."""
    path = runs.watch_stream_path(project)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and str(json.loads(line).get("run_id") or "") == run_id
    ]


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


def test_a_request_that_cannot_launch_is_not_retried_every_pass(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A request whose brief is missing fails once, then waits out a back-off.

    The request that cycled in the field was never waiting on the lane: its
    dispatch failed every pass because its brief file no longer existed, and each
    pass released it, failed and restored it. The first pass records that
    failure; the next two leave the pointer and its recorded failure untouched
    and append nothing to the stream.
    """
    _, resolve = _config_for()
    missing = tmp_path / "no-such-brief.md"
    request = _queue(admit_repo, node_id="no-brief", run_id="r-no-brief")
    request["node"]["brief"] = str(missing)
    request["node"]["brief_path"] = str(missing)
    runs.queue_dispatch(request)

    observed = []
    for _ in range(3):
        before = runs.pointer_path("r-no-brief").read_bytes()
        fleet_before = _fleet(PROJECT)

        result = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))

        after = runs.pointer_path("r-no-brief").read_bytes()
        rows = _stream_rows(fleet_before, _fleet(PROJECT))
        observed.append((before, after, result, rows))

    before_first, after_first, first, _ = observed[0]
    assert after_first != before_first, "the first pass must record the failure"
    assert "r-no-brief" in first["failed"]
    recorded = runs.read_pointer("r-no-brief")
    assert "not readable" in recorded["failure"]
    assert recorded.get("failure_reported_at"), "the first failure is reported"

    reports = _stream_file_rows(PROJECT, "r-no-brief")
    assert len(reports) == 1, "the session is told about the failure once"

    for before, after, result, rows in observed[1:]:
        assert after == before, "a backed-off request must be left unchanged"
        assert "r-no-brief" in result["held"]
        assert rows == [], "a backed-off request must append no transition"
    assert len(_stream_file_rows(PROJECT, "r-no-brief")) == 1, (
        "a later pass must add no second report"
    )


def test_a_lane_document_change_does_not_retry_a_request_that_cannot_launch(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The router's lane document is not a dependency of a failed request.

    The lane document is republished every few seconds, so watching it would
    retry a request that can never launch at the publication cadence. The
    back-off must hold across a lane-document change.
    """
    config, resolve = _config_for()
    lane = _lane_document(tmp_path, worker_slots=1)
    config["backends"]["clive"]["lane_document"] = str(lane)
    missing = tmp_path / "no-such-brief.md"
    request = _queue(admit_repo, node_id="no-brief", run_id="r-no-brief")
    request["node"]["brief"] = str(missing)
    request["node"]["brief_path"] = str(missing)
    runs.queue_dispatch(request)

    first = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    assert "r-no-brief" in first["failed"]
    settled = runs.pointer_path("r-no-brief").read_bytes()

    lane.write_text(json.dumps({"worker_slots": 2, "rev": 2}), encoding="utf-8")
    second = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    assert "r-no-brief" in second["held"], "a lane-document change must not retry"
    assert runs.pointer_path("r-no-brief").read_bytes() == settled


def test_a_launch_failed_requeue_is_not_retried_every_pass(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A run re-queued after a failed launch waits out a back-off, not a cycle.

    The drainer re-queues a run the reaper marked ``launch-failed`` by writing
    its stored request back as a queued pointer. That write carries the same
    bounded back-off a failed attempt records, so a launch that keeps failing is
    attempted once per back-off rather than re-queued on every pass.
    """
    _, resolve = _config_for()
    request = _queue(admit_repo, node_id="relaunch", run_id="r-relaunch")
    dead = dict(request)
    dead["phase"] = "launch-failed"
    dead["admit_request"] = request
    dead["launch_failures"] = [{"kind": "launch-failed"}]
    runs._write_json(runs.pointer_path("r-relaunch"), dead)

    first = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    assert "r-relaunch" in first["requeued"]
    settled = runs.pointer_path("r-relaunch").read_bytes()
    fleet_before = _fleet(PROJECT)

    second = queue_admission.admit_once(config_for=resolve, launcher=_launcher([]))
    assert "r-relaunch" in second["held"]
    assert runs.pointer_path("r-relaunch").read_bytes() == settled
    assert _stream_rows(fleet_before, _fleet(PROJECT)) == []
