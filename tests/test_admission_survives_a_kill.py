"""Interrupted admission retains every intent and adopts an existing worker."""

from __future__ import annotations

import collections
import contextlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch, queue_admission, runs
from reckon.crew.admission import interrupted
from reckon.crew.admission import service as admission_service
from reckon.crew.node import CrewError
from tests import test_a_queued_launch_keeps_the_run_record as fixtures
from tests import test_a_queued_run_launches_once as spawn_fixtures


@pytest.fixture
def admit_repo(tmp_path, monkeypatch):
    fixtures.harness_handshake.__wrapped__(monkeypatch)
    return fixtures.admit_repo.__wrapped__(tmp_path, monkeypatch)


def _worker_receipt(run_id, pid):
    runs._write_json(
        runs.run_dir(run_id) / "worker.json",
        {
            "run_id": run_id,
            "attempt": 1,
            "pid": pid,
            "pid_start_time": runs._process_start_time(pid),
            "launched_at": runs._utc_now(),
        },
    )


def _interrupted_pass(stage, channel, resolve):
    original = runs.write_handover

    def barrier():
        channel.send("barrier")
        signal.pause()

    def handover(pointer, **kwargs):
        # Freeze other launches before their handover so the pipe names one
        # interruption boundary; recovery must still conserve every request.
        if pointer["run_id"] != "r-interrupted-0":
            threading.Event().wait()
        if stage == "before-release":
            barrier()
        result = original(pointer, **kwargs)
        if stage == "after-release":
            barrier()
        if stage == "missing-pointer":
            runs.pointer_path(pointer["run_id"]).unlink()
            barrier()
        return result

    def launch(plan, *, log_path, **kwargs):
        if stage == "during-launch":
            barrier()
        run_id = Path(log_path).parent.name
        channel.send(("spawn", run_id))
        pid = channel.recv()
        _worker_receipt(run_id, pid)
        if stage == "worker-started":
            barrier()
        return pid

    runs.write_handover = handover
    if stage == "before-admission-stamp":
        queue_admission._stamp_admission = lambda *args, **kwargs: barrier()
    queue_admission.admit_once(config_for=resolve, launcher=launch)
    channel.send("finished")


@pytest.mark.parametrize(
    "stage",
    [
        "before-release",
        "after-release",
        "during-launch",
        "worker-started",
        "missing-pointer",
        "before-admission-stamp",
    ],
)
def test_a_killed_pass_keeps_every_request_and_launches_each_once(
    admit_repo, tmp_path, stage
):
    config, resolve = fixtures._config_for()
    fixtures._prime_lane(tmp_path, config, worker_slots=10)
    request_ids = {f"r-interrupted-{index}" for index in range(3)}
    for run_id in sorted(request_ids):
        runs.queue_dispatch(
            fixtures._queued_request(admit_repo, node_id=run_id, run_id=run_id)
        )
    workers = []
    launches = []

    def spawn(run_id):
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        workers.append(worker)
        launches.append(run_id)
        return worker.pid

    parent, child = multiprocessing.get_context("fork").Pipe()
    service = multiprocessing.get_context("fork").Process(
        target=_interrupted_pass, args=(stage, child, resolve)
    )
    service.start()
    try:
        assert parent.poll(30), "admission never reached its kill boundary"
        event = parent.recv()
        if isinstance(event, tuple):
            assert event[0] == "spawn"
            parent.send(spawn(event[1]))
            assert parent.poll(30)
            event = parent.recv()
        assert event == "barrier", event
        print(f"SIGKILL stage={stage} service_pid={service.pid}")
        os.kill(service.pid, signal.SIGKILL)
        service.join(10)
        assert service.exitcode == -signal.SIGKILL

        def launch(plan, *, log_path, **kwargs):
            run_id = Path(log_path).parent.name
            pid = spawn(run_id)
            _worker_receipt(run_id, pid)
            return pid

        fresh = queue_admission.admit_once(config_for=resolve, launcher=launch)
        rows = {row["run_id"]: row for row in runs.list_live()}
        assert set(rows) == request_ids, (stage, fresh, rows)
        assert all(
            row["phase"] in {"queued", "held", "working", "starting", "dispatched"}
            for row in rows.values()
        ), (stage, fresh, rows)
        for row in rows.values():
            if row["phase"] in {"starting", "dispatched"}:
                assert runs.record_process_alive(row, runs.process_alive) is True, (
                    "stranded request",
                    stage,
                    row,
                )
            assert runs.dispatch_field(row, "admit_request")["run_id"] == row["run_id"]
        assert collections.Counter(launches) == dict.fromkeys(request_ids, 1), (
            stage,
            launches,
            fresh,
        )
        again = queue_admission.admit_once(config_for=resolve, launcher=launch)
        assert again["launched"] == []
        assert collections.Counter(launches) == dict.fromkeys(request_ids, 1)
        print(
            f"recovered stage={stage} requests={len(rows)} unique_launches={len(launches)}"
        )
    finally:
        if service.is_alive():
            service.terminate()
        service.join(10)
        parent.close()
        child.close()
        for worker in workers:
            worker.terminate()
            worker.wait(timeout=10)


@pytest.mark.parametrize("moved", ["queued", "new-attempt"])
def test_a_delayed_supervisor_refuses_a_recovered_attempt(tmp_path, monkeypatch, moved):
    directory = spawn_fixtures.queued_run.__wrapped__(tmp_path, monkeypatch)
    spec = spawn_fixtures._spec(directory)
    original = dispatch_launch._worker_runtime_environment

    def recover_between_the_read_and_spawn(*args, **kwargs):
        pointer = runs.read_pointer(spawn_fixtures.RUN_ID)
        if moved == "queued":
            pointer["phase"] = "queued"
        else:
            pointer["attempt_started_at"] = "2026-11-08T00:00:00Z"
        runs._write_json(runs.pointer_path(spawn_fixtures.RUN_ID), pointer)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        dispatch_launch,
        "_worker_runtime_environment",
        recover_between_the_read_and_spawn,
    )
    stream = directory / "stream.jsonl"
    stream.write_text("retained stream\n")
    with pytest.raises(
        CrewError, match="no longer names this supervisor's launch attempt"
    ):
        dispatch_launch._supervisor_spawn_worker(spec)
    assert stream.read_text() == "retained stream\n"
    assert not (directory / "worker.json").exists()


def test_a_launch_supersedes_the_stamp_a_pointer_already_names(tmp_path, monkeypatch):
    """A pointer stamp older than this launch's own is the launch it replaces.

    A resume starts its supervisor before it rewrites the pointer, so the
    pointer still carries the dispatch-time instant at the attempt number the
    reservation keeps. That stamp names a launch this one has taken over, not
    another launch made after it, so the worker must start; refusing on it
    leaves the run unlaunchable.
    """
    directory = spawn_fixtures.queued_run.__wrapped__(tmp_path, monkeypatch)
    spec = spawn_fixtures._spec(directory)
    # The spec's own start is 2026-11-07T00:00:00Z, so this pointer stamp is
    # measured against it as an earlier launch's identity.
    spawn_fixtures._rewrite_pointer(attempt_started_at="2026-11-01T00:00:00Z")

    pid = dispatch_launch._supervisor_spawn_worker(spec)
    try:
        assert pid > 0, "the launch was refused on a stamp it supersedes"
        worker = json.loads((directory / "worker.json").read_text(encoding="utf-8"))
        assert worker["pid"] == pid, "the admitted launch wrote no worker record"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


@pytest.mark.parametrize("owner", ["live-local", "foreign"])
def test_recovery_keeps_a_handover_whose_owner_it_cannot_prove_dead(admit_repo, owner):
    request = fixtures._queued_request(admit_repo, node_id="owned", run_id="r-owned")
    runs.queue_dispatch(request)
    identity = admission_service._admitting_pass_identity()
    if owner == "foreign":
        identity.update(host="another-node.invalid", pid=999999999)
    runs.write_handover(request, admitted_by=identity)
    runs.pointer_path("r-owned").unlink()
    assert interrupted.recover() == []
    assert runs.find_handover("r-owned") is not None
    assert not runs.pointer_path("r-owned").exists()


def test_recovery_never_resurrects_a_recorded_discard(admit_repo):
    from reckon.crew import promotion

    request = fixtures._queued_request(
        admit_repo, node_id="discarded", run_id="r-discarded"
    )
    runs.queue_dispatch(request)
    identity = admission_service._admitting_pass_identity()
    identity.update(pid=spawn_fixtures._dead_pid(), pid_start_time=None)
    runs.write_handover(request, admitted_by=identity)
    promotion._write_discard_record("r-discarded", request)
    runs.pointer_path("r-discarded").unlink()
    assert promotion.discard_recorded("r-discarded")
    assert interrupted.recover() == []
    assert not runs.pointer_path("r-discarded").exists()
    assert runs.find_handover("r-discarded") is None


def test_recovery_adopts_the_supervisors_process_group_from_the_worker_receipt(
    tmp_path, monkeypatch
):
    directory = spawn_fixtures.queued_run.__wrapped__(tmp_path, monkeypatch)
    run_id = spawn_fixtures.RUN_ID
    request = runs.read_pointer(run_id)
    request.update(phase="queued", queued_at=runs._utc_now())
    request.pop("pid", None)
    request.pop("pid_start_time", None)
    identity = admission_service._admitting_pass_identity()
    identity.update(pid=spawn_fixtures._dead_pid(), pid_start_time=None)
    runs.write_handover(request, admitted_by=identity)
    spec = spawn_fixtures._spec(directory)
    spec["plan"]["argv"] = [
        sys.executable,
        "-c",
        'import time; print(\'{"type":"thread.started"}\', flush=True); time.sleep(120)',
    ]
    pid = dispatch_launch._supervisor_spawn_worker(spec)
    try:
        worker = json.loads((directory / "worker.json").read_text())
        assert worker["pid"] == pid
        assert worker["supervisor_pid"] == os.getpid()
        assert interrupted.recover() == [run_id]
        adopted = runs.read_pointer(run_id)
        assert adopted["pid"] == worker["supervisor_pid"]
        assert adopted["pid_start_time"] == worker["supervisor_pid_start_time"]
        assert adopted["phase"] == "working"
        assert runs.find_handover(run_id) is None
    finally:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
