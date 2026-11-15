"""Interrupted admission retains every intent and adopts an existing worker."""

from __future__ import annotations

import collections
import multiprocessing
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from tests import test_a_queued_launch_keeps_the_run_record as fixtures


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
