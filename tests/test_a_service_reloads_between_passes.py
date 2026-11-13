"""A reload preserves bounded work and prevents another pass on stale code."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import fleet_supervisor as fleet
from reckon.crew.admission import service as admission

STUB = r"""
import os, signal, sys, time
from pathlib import Path
from reckon.crew import fleet_supervisor as fleet
root, bound, idle = Path(sys.argv[1]), float(sys.argv[2]), bool(int(sys.argv[3]))
pid = os.getpid()
stamp = (root / "stamp").read_text()
runtime = Path(os.environ["FLEET_RUNTIME_DIR"])
fleet.write_service_pid(runtime, "demo", pid, fleet._lock_inode(runtime, "demo"), stamp)
active = False
def stop(*args):
    (root / f"stopped-{pid}").write_text(str(active))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
for number in range(2):
    with fleet.service_pass(bound=bound):
        active = True
        (root / f"begin-{pid}-{number}").write_text(stamp)
        while not (root / f"release-{number}").exists():
            time.sleep(0.01)
        (root / f"complete-{pid}-{number}").write_text(stamp)
        active = False
    (root / f"idle-{pid}").touch()
    while idle and not (root / "next").exists():
        time.sleep(0.01)
while True:
    time.sleep(0.01)
"""


def _wait(predicate):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    pytest.fail("controlled service did not reach the requested boundary")


@pytest.fixture()
def controlled(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    config = tmp_path / "config"
    runtime.mkdir()
    (config / "fleet").mkdir(parents=True)
    (tmp_path / "stamp").write_text("before")
    script = tmp_path / "stub.py"
    script.write_text(STUB)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    monkeypatch.setattr(fleet, "all_process_cmdlines", dict)
    managers = []

    def start(*, bound=30.0, idle=False):
        argv = [sys.executable, str(script), str(tmp_path), str(bound), str(int(idle))]
        (config / "fleet" / "services.json").write_text(json.dumps({"demo": argv}))
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(fleet.__file__).resolve().parents[2]),
            "FLEET_RUNTIME_DIR": str(runtime),
            "FLEET_STATE_DIR": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(config),
        }
        manager = fleet.DeclaredServices(
            runtime,
            env,
            code_stamp=(tmp_path / "stamp").read_text,
            checkout_trust=lambda: SimpleNamespace(trusted=True),
            stop_grace=1.0,
        )
        managers.append(manager)
        manager.reload()
        pid = fleet.recorded_service_pid(runtime, "demo")
        _wait((tmp_path / f"begin-{pid}-0").exists)
        return manager, pid

    yield SimpleNamespace(root=tmp_path, runtime=runtime, start=start)
    for manager in managers:
        manager.stop_all()


def test_a_stamp_change_finishes_the_pass_before_restarting(controlled):
    home = controlled
    manager, pid = home.start()
    (home.root / "stamp").write_text("after")
    manager.supervise_once()
    assert fleet.recorded_service_pid(home.runtime, "demo") == pid
    assert not (home.root / f"stopped-{pid}").exists()
    assert (home.runtime / "demo.pass.reload").exists()
    # A fresh supervisor must preserve the same active pass during adoption.
    state = manager._states.pop("demo")
    manager._release_image_pin(state)
    manager.reload()
    assert fleet.recorded_service_pid(home.runtime, "demo") == pid
    (home.root / "release-0").touch()
    _wait((home.root / f"complete-{pid}-0").exists)
    _wait((home.root / f"idle-{pid}").exists)
    manager.supervise_once()
    replacement = fleet.recorded_service_pid(home.runtime, "demo")
    assert replacement != pid
    marker = home.root / f"begin-{replacement}-0"
    _wait(marker.exists)
    assert marker.read_text() == "after"
    assert not (home.root / f"begin-{pid}-1").exists()
    assert (home.root / f"stopped-{pid}").read_text() == "False"


def test_a_stamp_change_between_passes_restarts_in_one_cycle(controlled):
    home = controlled
    manager, pid = home.start(idle=True)
    (home.root / "release-0").touch()
    _wait((home.root / f"idle-{pid}").exists)
    (home.root / "stamp").write_text("after")
    manager.supervise_once()
    replacement = fleet.recorded_service_pid(home.runtime, "demo")
    assert replacement != pid
    marker = home.root / f"begin-{replacement}-0"
    _wait(marker.exists)
    assert marker.read_text() == "after"
    assert (home.root / f"complete-{pid}-0").exists()


def test_an_overrunning_pass_is_restarted_and_logs_its_bound(controlled, capsys):
    home = controlled
    manager, pid = home.start(bound=0.2)
    record = json.loads((home.runtime / "demo.pass").read_text())
    _wait(lambda: time.monotonic() - record["started"] > record["bound"])
    (home.root / "stamp").write_text("after")
    manager.supervise_once()
    assert fleet.recorded_service_pid(home.runtime, "demo") != pid
    assert (home.root / f"stopped-{pid}").read_text() == "True"
    assert not (home.root / f"complete-{pid}-0").exists()
    output = capsys.readouterr().out
    assert "pass exceeded its 0.2s bound" in output


def test_admission_holds_the_boundary_through_verdict_publication(
    tmp_path, monkeypatch
):
    path = tmp_path / "demo.pass"
    monkeypatch.setenv(fleet.SERVICE_PASS_PATH_ENV, str(path))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    from reckon.crew import dispatch_launch

    monkeypatch.setattr(
        dispatch_launch, "_adopt_launched_workers_from_reexec", lambda: None
    )
    monkeypatch.setattr(admission, "_admit_code_stamp", lambda: "unchanged")
    monkeypatch.setattr(admission._release, "_release_tick", dict)
    worker = SimpleNamespace(tick=lambda: None, stop=lambda: None, last={})
    monkeypatch.setattr(admission._landing, "_LandingWorker", lambda *_: worker)
    observed = []

    def busy(*_args, **_kwargs):
        assert json.loads(path.read_text())["pid"] == os.getpid()
        with (
            path.with_suffix(".pass.lock").open("a+b") as handle,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed.append("busy")
        return {}

    def pause(_seconds):
        with path.with_suffix(".pass.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise KeyboardInterrupt

    monkeypatch.setattr(admission._selection, "admit_once", busy)
    monkeypatch.setattr(admission._verdicts, "_record_pass_verdicts", busy)
    monkeypatch.setattr(admission._verdicts, "_publish_pass_verdicts", busy)
    monkeypatch.setattr(admission, "_pause", pause)
    assert admission.admit_until_stopped() == {"passes": 1}
    assert observed == ["busy", "busy", "busy"]


def test_a_manual_pass_needs_no_supervisor_boundary(monkeypatch):
    monkeypatch.delenv(fleet.SERVICE_PASS_PATH_ENV, raising=False)
    with fleet.service_pass():
        pass
