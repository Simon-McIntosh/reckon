"""A standby supervisor takes its declared service lease only on promotion."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from reckon.crew import fleet_supervisor
from reckon.crew.host_lease import HostLease


def _wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("supervisor did not reach the expected state")


def _request(runtime: Path, verb: str):
    with (runtime / "requests").open("w", encoding="utf-8") as fifo:
        fifo.write(verb + "\n")


def test_standby_claims_declared_service_lease_only_on_promotion(tmp_path):
    runtime, state = tmp_path / "runtime", tmp_path / "state"
    config_home = tmp_path / "config"
    config = config_home / "fleet"
    config.mkdir(parents=True)
    (config / "services.json").write_text(
        json.dumps({"demo": [sys.executable, "-c", "import time; time.sleep(60)"]}),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "FLEET_RUNTIME_DIR": str(runtime),
        "FLEET_STATE_DIR": str(state),
        "XDG_CONFIG_HOME": str(config_home),
        "FLEET_HEALTH_SAMPLER": "",
        "SLURM_JOB_ID": "test-job",
        "PYTHONPATH": str(Path(fleet_supervisor.__file__).resolve().parents[2]),
    }
    supervisor_log = tmp_path / "supervisor.log"
    with supervisor_log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "reckon.crew.fleet_supervisor", "--standby"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
        )
    lease = HostLease(state, "demo", "observer", 0, "")
    try:
        _wait_for((runtime / "requests").exists)
        _request(runtime, "standby-ready-probe")
        _wait_for(
            lambda: (
                "unknown request: standby-ready-probe"
                in supervisor_log.read_text(encoding="utf-8")
            )
        )
        assert lease.holder() is None
        assert not lease.path.exists()
        _request(runtime, "promote")
        holder = _wait_for(lease.holder)
        assert (holder.host, holder.pid, holder.job) == (
            socket.gethostname().split(".")[0],
            process.pid,
            "test-job",
        )
        _wait_for(lambda: fleet_supervisor.recorded_service_pid(runtime, "demo"))
    finally:
        if process.poll() is None:
            _request(runtime, "stop")
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read() if process.stderr else ""
