"""A standby fleet reader serves requests before it takes ownership of the fleet."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from reckon.crew import fleet_node, fleet_supervisor


def _wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("the expected fleet action did not complete")


def _request(runtime: Path, line: str) -> None:
    with (runtime / "requests").open("w", encoding="utf-8") as fifo:
        fifo.write(line + "\n")


def test_standby_serves_then_promotes_record_and_declared_service(tmp_path):
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    config = tmp_path / "config" / "fleet"
    config.mkdir(parents=True)
    state.mkdir()
    record = state / "record.json"
    old_record = b'{"job_id":"old-job"}\n'
    record.write_bytes(old_record)
    service_marker = tmp_path / "service-started"
    service_script = tmp_path / "service.py"
    service_script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('started')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    (config / "services.json").write_text(
        json.dumps(
            {"demo": [sys.executable, str(service_script), str(service_marker)]}
        ),
        encoding="utf-8",
    )
    run = tmp_path / "run"
    run.mkdir()
    spawn_marker = tmp_path / "request-served"
    spec = run / "supervisor.json"
    spec.write_text(
        json.dumps(
            {
                "run_id": "r-test",
                "run_directory": str(run),
                "cwd": str(run),
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(spawn_marker)!r}).write_text('served')",
                ],
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "FLEET_RUNTIME_DIR": str(runtime),
        "FLEET_STATE_DIR": str(state),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "FLEET_HEALTH_SAMPLER": "",
        "SLURM_JOB_ID": "new-job",
        "PYTHONPATH": str(Path(fleet_supervisor.__file__).resolve().parents[2]),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "reckon.crew.fleet_supervisor", "--standby"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for((runtime / "requests").exists)
        _request(runtime, f"spawn r-test {spec}")
        _wait_for(spawn_marker.exists)
        assert (run / "spawned.json").exists()
        assert record.read_bytes() == old_record
        assert not service_marker.exists()
        assert fleet_supervisor.recorded_service_pid(runtime, "demo") is None

        _request(runtime, "promote")
        _wait_for(lambda: json.loads(record.read_text()).get("job_id") == "new-job")
        _wait_for(service_marker.exists)
        assert fleet_supervisor.recorded_service_pid(runtime, "demo") is not None
    finally:
        if process.poll() is None:
            _request(runtime, "stop")
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read() if process.stderr else ""


def test_hold_script_can_start_the_supervisor_in_standby():
    script = fleet_node.generate_hold_script(
        fleet_node.fleet_size(), log_path="fleet.log", standby=True
    )
    assert 'exec "$HOME/.local/bin/fleet-supervisor" --standby' in script


def test_reload_keeps_standby_without_applying_services(tmp_path):
    class Services:
        def reload(self):
            raise AssertionError("standby reload started declared services")

    invocations = []
    fleet_supervisor.handle_line(
        "reload",
        tmp_path,
        {},
        exec_=lambda _path, argv: invocations.append(argv),
        services=Services(),
        standby=True,
    )
    assert invocations == [
        [sys.executable, "-m", "reckon.crew.fleet_supervisor", "--standby"]
    ]
