"""A discarded request is not written back by the pass that was holding it.

The admission pass holds a queued request in memory while it attempts a launch
and writes the request back when the launch does not survive. A ``crew discard``
issued during that pass removes the pass's pointer and records the departure;
the restore then wrote the request back and undid the discard. A request that
never launched has no run directory, so its departure is recorded in the shared
discards spool instead of a run directory the discard must not create. These
tests bind the restore to that record: a discarded request stays gone whether or
not it has a run directory, and a request nobody discarded is still restored.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import importlib
import os
from pathlib import Path

from reckon.crew import promotion, queue_admission, runs
from reckon.crew.node import CrewError
from tests.test_a_held_request_stays_queued import _stream_file_rows
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    _config_for,
    _failing_launcher,
    _launcher,
    _queue,
    admit_repo,
)


def _attempted_request(repository: Path, run_id: str) -> None:
    """Queue a request that already has a run directory behind it.

    A queued request that has been attempted before carries a run directory,
    which is where a discard records its departure. The request in the field
    that came back had cycled through the queue, so its home already existed.
    """
    runs.queue_dispatch(_queue(repository, node_id="queued", run_id=run_id))
    runs.run_dir(run_id).mkdir(parents=True, exist_ok=True)


def _discard_mid_pass(monkeypatch) -> None:
    """Make the pass discard the request it holds, before its restore runs.

    The pass snapshots the queued pointer and then releases it inside
    ``_launch_queued``. Wrapping that step issues the discard after the snapshot
    but before the release, which is the window the field showed.
    """
    real_launch = queue_admission._launch_queued

    def discard_then_launch(pointer, **kwargs):
        # The operator discards the request mid-pass; its pointer is still the
        # one the pass is holding and has not yet released.
        promotion.discard(str(pointer["run_id"]))
        return real_launch(pointer, **kwargs)

    monkeypatch.setattr(queue_admission, "_launch_queued", discard_then_launch)


def test_a_request_discarded_during_the_pass_is_not_written_back(
    admit_repo: Path, monkeypatch
) -> None:
    """A discard made while the pass holds the request stands.

    The restore must read the discard's record and leave the pointer removed.
    """
    _, resolve = _config_for()
    run_id = "r-discarded"
    _attempted_request(admit_repo, run_id)

    _discard_mid_pass(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert promotion.discard_recorded(run_id), "the discard left a record"
    assert not runs.pointer_path(run_id).exists(), (
        "a discarded request must not be written back by the pass"
    )
    assert run_id in result["failed"]


def test_a_request_with_no_run_directory_is_found_discarded(
    admit_repo: Path, monkeypatch
) -> None:
    """A queued review that never launched has no run directory.

    The discard must leave a durable record the restore can read even then, and
    must not create the run directory to do it — recreating a run's home is what
    a discard exists to avoid.
    """
    _, resolve = _config_for()
    run_id = "r-no-home"
    runs.queue_dispatch(_queue(admit_repo, node_id="queued", run_id=run_id))
    assert not runs.run_dir(run_id).exists(), "the request never launched"

    _discard_mid_pass(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert not runs.pointer_path(run_id).exists(), (
        "a discarded request must not be written back by the pass"
    )
    assert promotion.discard_recorded(run_id), "the discard left a readable record"
    assert not runs.run_dir(run_id).exists(), "the discard must not create a run dir"
    assert run_id in result["failed"]


def test_a_request_discarded_before_its_spawn_is_not_launched(
    admit_repo: Path, monkeypatch
) -> None:
    """A launch admitted for a request already discarded starts no worker.

    The pass selects the request and releases it into the shared dispatch path,
    which publishes the run's claim before composing the launch and spawns the
    worker at the end of that path. An operator's discard landing inside the
    span removes the pointer and records the departure, and nothing on the
    launch path read that record: the launch wrote the run back and started a
    worker beside the record of its own discard, which every reader treating a
    recorded discard as an abandoned run then reads as a live abandoned run.
    """
    _, resolve = _config_for()
    run_id = "r-discarded-before-spawn"
    _attempted_request(admit_repo, run_id)

    _discard_mid_pass(monkeypatch)
    launched: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(launched)
    )

    assert promotion.discard_recorded(run_id), "the discard left a record"
    assert not runs.pointer_path(run_id).exists(), (
        f"a discarded request leaves no live run, and starts no worker "
        f"(launcher calls: {len(launched)})"
    )
    assert launched == [], "a discarded request starts no worker"
    assert run_id in result["failed"]


def test_a_discard_arriving_while_the_launch_composes_is_refused(
    admit_repo: Path, monkeypatch
) -> None:
    """A discard landing after the claim is published still stops the launch.

    The run's claim is on disk before the launch is composed, and the worker is
    started after it. ``_wire_peer_channels`` runs inside that span, so a
    discard issued here is the operator's own: the pointer goes, the departure
    is recorded in the run directory, and the launch must refuse rather than
    republish the pointer and start a worker. The record has to outlive the
    refusal's rollback, which removes that directory.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    _, resolve = _config_for()
    run_id = "r-discarded-while-composing"
    _attempted_request(admit_repo, run_id)

    real_wire = dispatch_module._wire_peer_channels

    def discard_then_wire(record, peers):
        promotion.discard(str(record["run_id"]))
        return real_wire(record, peers)

    monkeypatch.setattr(dispatch_module, "_wire_peer_channels", discard_then_wire)
    launched: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(launched)
    )

    assert not runs.pointer_path(run_id).exists(), (
        f"a discarded request leaves no live run (launcher calls: {len(launched)})"
    )
    assert launched == [], "a discarded request starts no worker"
    assert promotion.discard_recorded(run_id), (
        "the discard record outlives the rollback"
    )
    assert run_id in result["failed"]


def test_a_discard_landing_after_the_spawn_stops_the_worker(
    admit_repo: Path, monkeypatch
) -> None:
    """A discard inside the spawn leaves neither a running worker nor a lost record.

    The worker's pid reaches the pointer only after the process exists, so a
    discard landing between the spawn and that merge is refused by nothing: the
    pointer it reads names no process, it removes the pointer and records the
    departure in the run directory. The launch then has a worker no pointer
    names, and the rollback that follows would take the run directory — and the
    only discard record in it — with it.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    _, resolve = _config_for()
    run_id = "r-discarded-after-spawn"
    _attempted_request(admit_repo, run_id)

    real_start_time = dispatch_module._process_start_time

    def discard_then_read_start_time(pid):
        # The discard lands after the spawn and before the pid is merged onto
        # the pointer, which is the span the launch cannot see.
        promotion.discard(run_id)
        return real_start_time(pid)

    monkeypatch.setattr(
        dispatch_module, "_process_start_time", discard_then_read_start_time
    )
    stopped: list = []

    def record_stop(pid, start_time, **kwargs):
        stopped.append(pid)
        return []

    monkeypatch.setattr(dispatch_module, "_signal_process_group", record_stop)
    launched: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(launched)
    )

    assert len(launched) == 1, "the worker was spawned before the discard"
    assert stopped == [os.getpid()], f"the spawned worker was stopped: {stopped}"
    assert not runs.pointer_path(run_id).exists(), "no pointer names the worker"
    assert promotion.discard_recorded(run_id), (
        "the discard record outlives the rollback"
    )
    assert run_id in result["failed"]


def test_a_discard_outlives_a_refusal_by_another_guard(
    admit_repo: Path, monkeypatch
) -> None:
    """A departure survives a rollback caused by a guard other than the record check.

    The launch holds several guards after the claim is published — the fleet
    gate, the roster and scope checks — and any of them can refuse a launch that
    has already composed past a discard. The record check never runs then, so
    nothing else copies the marker out of the run directory, and the rollback
    removes that directory. The claim release copies it into the shared spool
    first, so the departure is still readable after the refusal.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    _, resolve = _config_for()
    run_id = "r-discarded-refused-at-the-gate"
    _attempted_request(admit_repo, run_id)

    real_wire = dispatch_module._wire_peer_channels

    def discard_then_wire(record, peers):
        promotion.discard(str(record["run_id"]))
        return real_wire(record, peers)

    monkeypatch.setattr(dispatch_module, "_wire_peer_channels", discard_then_wire)

    def refuse_at_the_gate():
        raise CrewError("the fleet gate is closed")

    monkeypatch.setattr(dispatch_module, "_require_fleet_gate_open", refuse_at_the_gate)
    launched: list = []

    result = queue_admission.admit_once(
        config_for=resolve, launcher=_launcher(launched)
    )

    assert launched == [], "a refused launch starts no worker"
    assert promotion.discard_recorded(run_id), (
        "the discard record outlives a rollback the record check never ran at"
    )
    assert not runs.pointer_path(run_id).exists(), "the refusal leaves no live run"
    assert not runs.run_dir(run_id).exists(), "the rollback removes the run directory"
    assert run_id in result["failed"]


def test_a_request_nobody_discarded_is_still_restored(admit_repo: Path) -> None:
    """A failed launch whose request nobody discarded is written back queued."""
    _, resolve = _config_for()
    run_id = "r-kept"
    _attempted_request(admit_repo, run_id)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert not promotion.discard_recorded(run_id)
    pointer = runs.read_pointer(run_id)
    assert pointer["phase"] == "queued"
    assert run_id in result["failed"]


def test_a_discarded_request_is_reported_nowhere(admit_repo: Path, monkeypatch) -> None:
    """The first-failure report is silent for a request the operator discarded.

    The report appends one row naming the failed run so a watching session sees
    it. A request discarded during the pass has left the fleet, so a row for it
    names a run nobody holds and reports a failure nobody is waiting on.
    """
    _, resolve = _config_for()
    run_id = "r-discarded-silent"
    _attempted_request(admit_repo, run_id)

    _discard_mid_pass(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    assert promotion.discard_recorded(run_id), "the discard left a record"
    assert _stream_file_rows(PROJECT, run_id) == [], (
        "a discarded request is reported nowhere"
    )
    assert not runs.pointer_path(run_id).exists(), (
        "a discarded request must not be written back by the pass"
    )
    assert run_id in result["failed"]


def test_a_kept_request_is_reported_once_as_queued(admit_repo: Path) -> None:
    """A failed launch nobody discarded appends exactly one queued row.

    The row is the session's first sight of the failure, and the pointer's
    ``failure_reported_at`` is what stops the next pass appending a second.
    """
    _, resolve = _config_for()
    run_id = "r-kept-reported"
    _attempted_request(admit_repo, run_id)

    result = queue_admission.admit_once(config_for=resolve, launcher=_failing_launcher)

    rows = _stream_file_rows(PROJECT, run_id)
    assert len(rows) == 1, f"expected one row naming the run, got {rows}"
    assert rows[0]["to_state"] == "queued"
    pointer = runs.read_pointer(run_id)
    assert pointer.get("failure_reported_at"), "the report marker is on the pointer"
    assert run_id in result["failed"]
