"""A declared address-space bound isolates a service's failed allocation."""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import fleet_supervisor as fleet

LIMIT = 768 * 1024 * 1024
SOURCE = """import json, os, resource, sys, time
from pathlib import Path
root, mode = Path(sys.argv[1]), sys.argv[2]
root.mkdir(exist_ok=True)
if os.environ.get('RECKON_MEMORY_REHEARSAL') == '1':
    Path('/proc/self/oom_score_adj').write_text('1000')
copies = list(root.iterdir())
(root / str(os.getpid())).write_text(json.dumps(list(resource.getrlimit(resource.RLIMIT_AS))))
while not (root.parent / 'go').exists():
    time.sleep(0.02)
if mode == 'grow' and not copies:
    data = []
    while True:
        data.append(bytearray(b"x" * (64 * 1024 * 1024)))
while True:
    time.sleep(0.02)
"""


def wait_for(read, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = read()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("service did not reach the expected state")


@pytest.fixture
def home(tmp_path):
    script = tmp_path / "service.py"
    script.write_text(SOURCE)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "FLEET_STATE_DIR": str(tmp_path / "state"),
        "FLEET_RUNTIME_DIR": str(runtime),
        "FLEET_NAME": "fleet",
        "PYTHONPATH": str(Path(fleet.__file__).resolve().parents[2]),
    }
    config = fleet.services_config_path(env)
    config.parent.mkdir(parents=True)
    clock = [0.0]
    manager = fleet.DeclaredServices(
        runtime, env, clock=lambda: clock[0], code_stamp=lambda: "test"
    )
    manager._code_decision = lambda *_: "adopt"
    yield tmp_path, script, config, manager, clock
    manager.stop_all()


def declare(home, limited=True):
    root, script, config, manager, _ = home
    first = {"argv": [sys.executable, str(script), str(root / "grow"), "grow"]}
    if limited:
        first["memory_limit_bytes"] = LIMIT
    config.write_text(
        json.dumps(
            {
                "grow": first,
                "peer": [sys.executable, str(script), str(root / "peer"), "idle"],
            }
        )
    )
    manager.reload()
    for name in ("grow", "peer"):
        wait_for(lambda name=name: list((root / name).glob("*")))
    return {name: manager._states[name].pid for name in ("grow", "peer")}


def test_declared_limit_reaches_the_service_process(home):
    root, _, _, _, _ = home
    pids = declare(home)
    assert json.loads((root / "grow" / str(pids["grow"])).read_text()) == [LIMIT, LIMIT]


def test_no_declaration_preserves_the_inherited_limit(home):
    root, _, _, _, _ = home
    home[3]._environ["RECKON_SERVICE_MEMORY_LIMIT_BYTES"] = str(LIMIT)
    pids = declare(home, limited=False)
    inherited = list(resource.getrlimit(resource.RLIMIT_AS))
    for name, pid in pids.items():
        assert json.loads((root / name / str(pid)).read_text()) == inherited


def test_a_failed_allocation_restarts_only_its_service_after_backoff(home, capsys):
    root, _, _, manager, clock = home
    pids = declare(home)
    assert json.loads((root / "grow" / str(pids["grow"])).read_text()) == [LIMIT, LIMIT]
    (root / "go").touch()
    wait_for(lambda: not manager._alive("grow", manager._states["grow"]))
    clock[0] = 1.0
    manager.supervise_once()
    assert manager._states["grow"].next_attempt == 1.0 + fleet.FIRST_BACKOFF_SECONDS
    clock[0] = fleet.FIRST_BACKOFF_SECONDS + 0.9
    manager.supervise_once()
    assert len(list((root / "grow").iterdir())) == 1
    clock[0] += 0.2
    manager.supervise_once()
    wait_for(lambda: len(list((root / "grow").iterdir())) == 2)
    assert manager._states["grow"].pid != pids["grow"]
    assert manager._states["peer"].pid == pids["peer"]
    assert manager._alive("peer", manager._states["peer"])
    output = capsys.readouterr().out
    assert "grow exited" in output
    assert f"memory_limit_bytes={LIMIT}" in output
    assert "child-tree" in output
    assert "MemoryError" in (root / "state" / "services" / "grow.log").read_text()


@pytest.mark.parametrize("limit", [0, -1, True, "768", 1.5, sys.maxsize + 1])
def test_invalid_limits_refuse_the_config_without_stopping_services(
    home, limit, capsys
):
    _, _, config, manager, _ = home
    pids = declare(home)
    entries = json.loads(config.read_text())
    entries["grow"]["memory_limit_bytes"] = limit
    config.write_text(json.dumps(entries))
    manager.reload()
    assert {name: state.pid for name, state in manager._states.items()} == pids
    assert "memory_limit_bytes must be a positive integer" in capsys.readouterr().out


def test_a_limit_change_restarts_only_the_changed_service(home):
    root, _, config, manager, _ = home
    pids = declare(home, limited=False)
    entries = json.loads(config.read_text())
    entries["grow"]["memory_limit_bytes"] = LIMIT
    config.write_text(json.dumps(entries))
    manager.reload()
    wait_for(lambda: len(list((root / "grow").iterdir())) == 2)
    assert manager._states["peer"].pid == pids["peer"]
    assert manager._states["grow"].pid != pids["grow"]
    assert json.loads(
        (root / "grow" / str(manager._states["grow"].pid)).read_text()
    ) == [LIMIT, LIMIT]


def test_a_declared_limit_applies_when_adopting_an_unbounded_copy(home):
    root, _, config, manager, _ = home
    pids = declare(home, limited=False)
    entries = json.loads(config.read_text())
    entries["grow"]["memory_limit_bytes"] = LIMIT
    config.write_text(json.dumps(entries))
    adopting = fleet.DeclaredServices(
        manager._runtime, manager._environ, code_stamp=lambda: "test"
    )
    adopting._code_decision = lambda *_: "adopt"
    try:
        adopting.reload()
        wait_for(lambda: len(list((root / "grow").iterdir())) == 2)
        assert adopting._states["grow"].pid != pids["grow"]
        assert json.loads(
            (root / "grow" / str(adopting._states["grow"].pid)).read_text()
        ) == [LIMIT, LIMIT]
    finally:
        adopting.stop_all()


def _job_memory_events():
    relative = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
    path = Path("/sys/fs/cgroup") / relative.lstrip("/")
    for directory in (path, *path.parents):
        limit = directory / "memory.max"
        if limit.exists() and limit.read_text().strip() == str(2 * 1024**3):
            events = dict(
                line.split()
                for line in (directory / "memory.events").read_text().splitlines()
            )
            return directory, int(events["oom_kill"])
    pytest.fail("rehearsal must run in its own 2 GiB job cgroup")


@pytest.mark.skipif(
    os.environ.get("RECKON_MEMORY_REHEARSAL") != "1",
    reason="requires a dedicated 2 GiB allocation",
)
def test_a_bounded_service_preserves_its_peer_and_the_job_kill_count(home, capsys):
    root, _, config, manager, clock = home
    negative = os.environ.get("RECKON_MEMORY_NEGATIVE_CONTROL") == "1"
    pids = declare(home, limited=not negative)
    if negative:
        # Both stand-ins allocate beyond the job bound in the unbounded control.
        entries = json.loads(config.read_text())
        entries["peer"][-1] = "grow"
        manager.stop_all()
        config.write_text(json.dumps(entries))
        for directory in (root / "grow", root / "peer"):
            for marker in directory.iterdir():
                marker.unlink()
        manager.reload()
        for name in ("grow", "peer"):
            wait_for(lambda name=name: list((root / name).iterdir()))
        pids = {name: state.pid for name, state in manager._states.items()}
    directory, before = _job_memory_events()
    (root / "go").touch()
    wait_for(lambda: not manager._alive("grow", manager._states["grow"]), timeout=90)
    if negative:
        wait_for(
            lambda: not manager._alive("peer", manager._states["peer"]), timeout=90
        )
    _, after = _job_memory_events()
    peer_alive = manager._alive("peer", manager._states["peer"])
    with capsys.disabled():
        print(
            json.dumps(
                {
                    "job": os.environ.get("SLURM_JOB_ID"),
                    "cgroup": str(directory),
                    "memory_max_bytes": 2 * 1024**3,
                    "memory_limit_bytes": None if negative else LIMIT,
                    "pids": pids,
                    "peer_alive": peer_alive,
                    "oom_kill_before": before,
                    "oom_kill_after": after,
                }
            ),
            flush=True,
        )
    assert peer_alive, "the unbounded control lost its peer"
    assert after == before, "the service reached the job OOM instead of its own bound"
    clock[0] = 1.0
    manager.supervise_once()
    clock[0] += fleet.FIRST_BACKOFF_SECONDS + 0.1
    manager.supervise_once()
    wait_for(lambda: len(list((root / "grow").iterdir())) == 2)
    assert manager._states["peer"].pid == pids["peer"]
    assert manager._states["grow"].pid != pids["grow"]
    with capsys.disabled():
        print(
            json.dumps(
                {
                    "restarted_pid": manager._states["grow"].pid,
                    "peer_pid_after": manager._states["peer"].pid,
                }
            ),
            flush=True,
        )


def test_a_refused_process_limit_never_executes_the_service(home, monkeypatch, capsys):
    _, _, _, manager, _ = home
    env = {**manager._environ, "RECKON_SERVICE_MEMORY_LIMIT_BYTES": str(LIMIT)}
    monkeypatch.setattr(fleet.oom_priority, "claim_service", lambda: None)
    monkeypatch.setattr(fleet, "_source_code_stamp", lambda: "test")

    def refuse(*_):
        raise PermissionError("limit refused by kernel")

    monkeypatch.setattr(resource, "setrlimit", refuse)

    def unexpected_exec(*_):
        pytest.fail("service executed after its limit was refused")

    monkeypatch.setattr(os, "execvp", unexpected_exec)
    assert fleet.run_declared_service("refused", ["unused"], env) == 2
    assert "cannot apply memory_limit_bytes=" in capsys.readouterr().out
    assert not fleet.lock_held("refused", manager._runtime)
