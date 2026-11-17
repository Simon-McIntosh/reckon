"""A pass fills a router grant concurrently and drains before code replacement."""

from __future__ import annotations

import copy
import os
import threading
import time
from contextvars import ContextVar
from pathlib import Path

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


def test_a_pass_reserves_the_router_allowance_before_launching(admission, monkeypatch):
    resolve, queued = admission
    lane = resolve({})["backends"]["clive"]["lane_document"]
    fixtures._write_lane(Path(lane), fixtures._session_lane(2))
    completed = []
    monkeypatch.setattr(
        queue_admission, "_launch_queued", _launch_after(0.2, completed)
    )
    result = queue_admission.admit_once(config_for=resolve)
    assert result["launched"] == queued[:2]
    assert result["held"] == queued[2:]
    assert len(completed) == 2
    assert all(
        runs.read_pointer(identity)["phase"] == "queued" for identity in queued[2:]
    )


def test_a_refused_launch_returns_its_reserved_allowance(admission, monkeypatch):
    resolve, queued = admission
    lane = resolve({})["backends"]["clive"]["lane_document"]
    fixtures._write_lane(Path(lane), fixtures._session_lane(1))
    completed = []
    launch = _launch_after(0.1, completed)

    def refusing(pointer, **kwargs):
        if pointer["run_id"] == queued[0]:
            raise RuntimeError("the configured executable cannot be started")
        return launch(pointer, **kwargs)

    monkeypatch.setattr(queue_admission, "_launch_queued", refusing)
    result = queue_admission.admit_once(config_for=resolve)
    assert result["failed"] == queued[:1]
    assert result["launched"] == queued[1:2]
    assert result["held"] == queued[2:]
    assert runs.read_pointer(queued[0])["phase"] == "queued"
    assert "executable cannot be started" in result["failure_detail"][queued[0]]


def test_the_admit_service_stops_after_its_launches_finish(
    admission, tmp_path, monkeypatch
):
    import json
    import sys

    resolve, queued = admission
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config_home = tmp_path / "supervisor-config"
    (config_home / "fleet").mkdir(parents=True)
    script = tmp_path / "admit_service.py"
    script.write_text(
        """
import json, os, signal, sys, time
from pathlib import Path
from reckon.crew import fleet_supervisor, queue_admission, runs
root = Path(sys.argv[1])
(root / "fleet-state.txt").write_text(str(fleet_supervisor.state_directory()))
config = json.loads(sys.argv[2])
def stop(*args):
    (root / "stopped.json").write_text(json.dumps({
        "began": sorted(p.name.removeprefix("began-") for p in root.glob("began-*")),
        "completed": sorted(p.name.removeprefix("completed-") for p in root.glob("completed-*")),
    }))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
def launch(pointer, **kwargs):
    identity = pointer["run_id"]
    (root / ("began-" + identity)).touch()
    time.sleep(0.5)
    record = runs._mutate_pointer(identity, lambda row: {**row, "phase": "working"})
    (root / ("completed-" + identity)).touch()
    return record
queue_admission._launch_queued = launch
queue_admission._admit_code_stamp = lambda: "running-image"
queue_admission.admit_until_stopped(
    interval=0.01, config_for=lambda pointer: config, lander=lambda: {}
)
"""
    )
    argv = [sys.executable, str(script), str(tmp_path), json.dumps(resolve({}))]
    (config_home / "fleet" / "services.json").write_text(json.dumps({"admit": argv}))
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(fleet.__file__).resolve().parents[2]),
        "FLEET_RUNTIME_DIR": str(runtime),
        "XDG_CONFIG_HOME": str(config_home),
    }
    from types import SimpleNamespace

    manager = fleet.DeclaredServices(
        runtime,
        environment,
        code_stamp=lambda: "replacement-image",
        checkout_trust=lambda: SimpleNamespace(trusted=True),
        stop_grace=2,
    )
    monkeypatch.setattr(fleet, "all_process_cmdlines", dict)

    def wait_for(predicate):
        until = time.monotonic() + 15
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.01)
        pytest.fail("admission service did not reach its requested boundary")

    try:
        manager.reload()
        pid = fleet.recorded_service_pid(runtime, "admit")
        wait_for(lambda: bool(list(tmp_path.glob("began-*"))))
        assert (
            Path((tmp_path / "fleet-state.txt").read_text()) == fleet.state_directory()
        )
        manager.supervise_once()
        assert fleet.recorded_service_pid(runtime, "admit") == pid
        assert not (tmp_path / "stopped.json").exists()
        wait_for(lambda: len(list(tmp_path.glob("completed-*"))) == len(queued))
        wait_for(lambda: manager._pass_allows_restart("admit", pid))
        from click.testing import CliRunner

        from reckon import cli
        from reckon.crew.follower_reload import service_reload_status

        schedule = service_reload_status("admit")
        assert schedule["running_code"] == "running-image"
        assert schedule["tick_exported_at"] is not None
        assert schedule["next_reload_at"] > time.time()
        manager.supervise_once()
        assert fleet.recorded_service_pid(runtime, "admit") == pid
        assert not (tmp_path / "stopped.json").exists()
        requested = CliRunner().invoke(
            cli.main,
            ["crew", "reload", "admit", "--reason", "replace after launch drain"],
        )
        assert requested.exit_code == 0, requested.output
        receipt = json.loads(requested.output)
        assert receipt["reason"] == "replace after launch drain"
        assert receipt["requested_by"]
        manager.supervise_once()
        wait_for((tmp_path / "stopped.json").exists)
        stopped = json.loads((tmp_path / "stopped.json").read_text())
        assert stopped == {"began": sorted(queued), "completed": sorted(queued)}
        print(
            f"service_restart_began={len(stopped['began'])} completed={len(stopped['completed'])}"
        )
    finally:
        manager.stop_all()


def test_an_exhausted_session_does_not_delay_another_sessions_slots(
    admission, monkeypatch
):
    resolve, queued = admission
    lane = Path(resolve({})["backends"]["clive"]["lane_document"])
    payload = fixtures._session_lane(1)
    payload["admission"]["sessions"]["another-session"] = {"worker_slots": 4}
    fixtures._write_lane(lane, payload)
    for identity in queued[2:]:
        runs._mutate_pointer(
            identity, lambda row: {**row, "session": "another-session"}
        )
    # Explicit priorities can place a session's tail before another session.
    monkeypatch.setattr(
        queue_admission,
        "fair_share_admission_order",
        lambda rows, *args, **kwargs: sorted(rows, key=lambda row: row["run_id"]),
    )
    other_began = threading.Event()
    completed = []
    plain = _launch_after(0, completed)

    def launch(pointer, **kwargs):
        if pointer["session"] == fixtures.SESSION:
            assert other_began.wait(5), (
                "another session waited behind an in-flight launch"
            )
        else:
            other_began.set()
        return plain(pointer, **kwargs)

    monkeypatch.setattr(queue_admission, "_launch_queued", launch)
    result = queue_admission.admit_once(config_for=resolve)
    assert result["failed"] == []
    assert result["launched"] == [queued[0], *queued[2:]]
    assert result["held"] == queued[1:2]
