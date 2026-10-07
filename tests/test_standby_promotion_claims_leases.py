"""A standby supervisor takes its declared service lease only on promotion."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from reckon.crew import fleet_supervisor
from reckon.crew.host_lease import HostLease
from tests.fleet_supervisor_requests import ready_response, send_request, wait_for


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
    process = subprocess.Popen(
        [sys.executable, "-m", "reckon.crew.fleet_supervisor", "--standby"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    lease = HostLease(state, "demo", "observer", 0, "")
    try:
        wait_for((runtime / "requests").exists)
        ready = ready_response(runtime, state, "b" * 32)
        assert ready["standby"] is True
        assert ready["job_id"] == "test-job"
        assert lease.holder() is None
        assert not lease.path.exists()
        send_request(runtime, "promote")
        holder = wait_for(lease.holder)
        assert (holder.host, holder.pid, holder.job) == (
            socket.gethostname().split(".")[0],
            process.pid,
            "test-job",
        )
        wait_for(lambda: fleet_supervisor.recorded_service_pid(runtime, "demo"))
    finally:
        if process.poll() is None:
            send_request(runtime, "stop")
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read() if process.stderr else ""
