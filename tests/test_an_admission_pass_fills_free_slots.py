"""A pass fills a router grant concurrently and drains before code replacement."""

from __future__ import annotations

import copy
import os
import threading
import time
from contextvars import ContextVar

import pytest

from reckon.crew import fleet_supervisor as fleet
from reckon.crew import queue_admission, runs
from tests import test_each_admission_pass_records_its_verdict as fixtures


@pytest.fixture()
def admission(tmp_path, monkeypatch):
    repository = fixtures.admit_repo.__wrapped__(tmp_path, monkeypatch)
    lane = tmp_path / "lane.json"
    fixtures._write_lane(lane, fixtures._session_lane(6))
    _, resolve = fixtures._config_with_lane(lane)
    queued = []
    for index in range(6):
        pointer = copy.deepcopy(fixtures._queue(repository))
        pointer["run_id"] = f"r-fill-{index}"
        pointer["node"]["id"] = f"fill-{index}"
        pointer["node"]["write_paths"] = [f"fill-{index}.txt"]
        runs.queue_dispatch(pointer)
        queued.append(pointer["run_id"])
    return resolve, queued


def _launch_after(seconds, completed, started=None):
    def launch(pointer, **kwargs):
        if started is not None:
            started.set()
        time.sleep(seconds)
        run_id = pointer["run_id"]
        record = runs._mutate_pointer(run_id, lambda row: {**row, "phase": "working"})
        completed.append((run_id, time.monotonic()))
        return record

    return launch


def test_six_twenty_second_launches_complete_within_one_minute(admission, monkeypatch):
    resolve, queued = admission
    completed = []
    monkeypatch.setattr(queue_admission, "_launch_queued", _launch_after(20, completed))
    started = time.monotonic()
    result = queue_admission.admit_once(config_for=resolve, budget_seconds=180)
    elapsed = time.monotonic() - started
    print(f"launches={len(completed)} elapsed_seconds={elapsed:.3f}")
    assert set(result["launched"]) == set(queued)
    assert len(completed) == 6
    assert max(at - started for _, at in completed) < 60
    assert all(runs.read_pointer(run_id)["phase"] == "working" for run_id in queued)


def test_code_restart_waits_for_every_started_launch(admission, tmp_path, monkeypatch):
    resolve, queued = admission
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "admit.pass"
    monkeypatch.setenv(fleet.SERVICE_PASS_PATH_ENV, str(path))
    manager = fleet.DeclaredServices(runtime, {})
    completed = []
    began = threading.Event()
    monkeypatch.setattr(
        queue_admission, "_launch_queued", _launch_after(0.5, completed, began)
    )
    stopped_after = []

    def restart():
        assert began.wait(15)
        assert not manager._pass_allows_restart("admit", os.getpid())
        assert path.with_suffix(".pass.reload").exists()
        while not manager._pass_allows_restart("admit", os.getpid()):
            time.sleep(0.01)
        stopped_after.append(len(completed))

    reloader = threading.Thread(target=restart)
    reloader.start()
    try:
        with fleet.service_pass():
            result = queue_admission.admit_once(config_for=resolve)
        reloader.join(15)
        assert not reloader.is_alive()
    finally:
        reloader.join(15)
    print(
        f"restart_completed_launches={stopped_after} launched={len(result['launched'])}"
    )
    assert stopped_after == [len(queued)]
    assert set(result["launched"]) == set(queued)


def test_launches_inherit_the_pass_read_context(admission, monkeypatch):
    resolve, queued = admission
    context = ContextVar("admission-test-context", default=None)
    token = context.set("pass-read")
    seen = []
    completed = []
    launch = _launch_after(0, completed)

    def contextual(pointer, **kwargs):
        seen.append(context.get())
        return launch(pointer, **kwargs)

    monkeypatch.setattr(queue_admission, "_launch_queued", contextual)
    try:
        result = queue_admission.admit_once(config_for=resolve)
    finally:
        context.reset(token)
    assert len(result["launched"]) == len(queued)
    assert seen == ["pass-read"] * len(queued)
