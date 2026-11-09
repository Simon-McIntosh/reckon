"""A requeued fleet job's standby supervisor promotes itself and resumes service.

A requeued allocation keeps its job id and starts its batch script again on the
node SLURM moves it to, so a hold script starts the supervisor in standby a
second time. When the published fleet record already names that job and SLURM
reports a restart, the start continues the fleet the dead node hosted rather than
holding for a migration that is over: it publishes its own record, takes over the
station and starts the declared services. A standby whose record names another
job is a migration still in progress and starts nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from reckon.crew import fleet_supervisor
from tests.fleet_supervisor_requests import send_request, wait_for

ROOT = Path(fleet_supervisor.__file__).resolve().parents[2]

# The driver runs one real ``serve`` loop in its own process, so the loop's
# task-limit change, its child reaping and its health sampler stay out of the
# test process. The station and the declared services are the two starters this
# case is about, so both are replaced with recorders of their own calls.
DRIVER = """
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from reckon.crew import fleet_supervisor

config = json.loads(Path(sys.argv[2]).read_text())
environ = dict(config["environ"])
calls = Path(config["calls"])


def record(name):
    seen = set(calls.read_text(encoding="utf-8").split()) if calls.exists() else set()
    seen.add(name)
    calls.write_text("\\n".join(sorted(seen)) + "\\n", encoding="utf-8")


class Station:
    def __init__(self, runtime, environ):
        pass

    def ensure(self):
        record("station")

    def stop(self):
        pass


class Services:
    def __init__(self, runtime, environ):
        pass

    def reload(self):
        record("services")

    def supervise_loop(self, stopping):
        stopping.wait()

    def stop_all(self):
        pass


fleet_supervisor.StationProcess = Station
fleet_supervisor.DeclaredServices = Services
fleet_supervisor.start_health_sampler = lambda environ: None

raise SystemExit(fleet_supervisor.serve(environ, standby=True))
"""


class Running:
    """One driven standby supervisor and the files a case reads it through."""

    def __init__(
        self, tmp_path: Path, *, record_job: str, job_id: str, restart_count: str
    ) -> None:
        self.runtime = tmp_path / "runtime"
        self.state = tmp_path / "state"
        self.calls = tmp_path / "calls"
        self.record = self.state / fleet_supervisor.RECORD_NAME
        self.state.mkdir()
        (tmp_path / "shm").mkdir()
        (tmp_path / "config").mkdir()
        self.record.write_text(
            json.dumps(
                {
                    "job_id": record_job,
                    "node": "some-old-node",
                    "runtime_dir": "/run/of/the/dead/node",
                    "started_at": "2026-11-09T08:03:00Z",
                }
            ),
            encoding="utf-8",
        )
        environ = {
            **os.environ,
            "FLEET_RUNTIME_DIR": str(self.runtime),
            "FLEET_STATE_DIR": str(self.state),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "FLEET_SESSION_TMP_ROOT": str(tmp_path / "shm"),
            "FLEET_HEALTH_SAMPLER": "",
            "SLURM_JOB_ID": job_id,
            "SLURM_RESTART_COUNT": restart_count,
            "PYTHONPATH": str(ROOT),
        }
        specification = tmp_path / "driver.json"
        specification.write_text(
            json.dumps({"environ": environ, "calls": str(self.calls)}),
            encoding="utf-8",
        )
        driver = tmp_path / "driver.py"
        driver.write_text(DRIVER, encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(driver), str(ROOT), str(specification)],
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def started(self) -> set[str]:
        if not self.calls.exists():
            return set()
        return set(self.calls.read_text(encoding="utf-8").split())

    def stop(self) -> None:
        """Stop the reader through its own request, then collect its output."""
        try:
            if self.process.poll() is None:
                wait_for((self.runtime / "requests").exists)
                send_request(self.runtime, "stop")
                self.process.wait(timeout=15)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            out, err = self.process.communicate()
        assert self.process.returncode == 0, (out, err)


def test_a_standby_restart_of_the_recorded_job_promotes_and_starts_its_services(
    tmp_path,
):
    """The restart publishes its own record and starts the station and services.

    The record the dead node published names this job, and SLURM reports the
    restart, so this start is the fleet continuing: it must publish a record
    naming this host and start both starters rather than standing by.
    """
    run = Running(tmp_path, record_job="1291586", job_id="1291586", restart_count="1")
    run.stop()

    published = json.loads(run.record.read_text(encoding="utf-8"))
    assert published["job_id"] == "1291586"
    assert published["node"] == fleet_supervisor._short_hostname()
    assert run.started() == {"services", "station"}


def test_a_standby_whose_record_names_another_job_starts_nothing(tmp_path):
    """A migration still in progress keeps its standby: no record, no starters."""
    run = Running(tmp_path, record_job="1291000", job_id="1291586", restart_count="1")
    before = run.record.read_bytes()
    run.stop()

    assert run.record.read_bytes() == before
    assert run.started() == set()


def test_a_fresh_start_of_the_recorded_job_stays_in_standby(tmp_path):
    """A first start of the recorded job is not a restart, so it holds."""
    run = Running(tmp_path, record_job="1291586", job_id="1291586", restart_count="0")
    before = run.record.read_bytes()
    run.stop()

    assert run.record.read_bytes() == before
    assert run.started() == set()
