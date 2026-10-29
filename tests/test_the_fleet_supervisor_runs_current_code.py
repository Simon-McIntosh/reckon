"""A running fleet supervisor replaces its image when the checkout moves.

The supervisor outlives every session on the node, so a fix to the code it runs
reaches the node only when it notices that the checkout has moved on. It compares
its recorded code stamp with the checkout's on each pass and re-executes in place
through the same reload path a manual request takes -- unless the checkout is
mid-edit, in which case it keeps running the image it has.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from reckon.crew import fleet_supervisor
from tests.fleet_supervisor_requests import send_request, wait_for

ROOT = Path(fleet_supervisor.__file__).resolve().parents[2]

# The driver runs one real ``serve`` loop in its own process, so the loop's
# child reaping and its task-limit change stay out of the test process. Only the
# three things a test must control enter by injection: the process replacement,
# the recorded stamp and the trusted-checkout verdict.
DRIVER = '''
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from reckon.crew import fleet_supervisor

config = json.loads(Path(sys.argv[2]).read_text())
environ = dict(config["environ"])
runtime = Path(config["runtime"])
calls = Path(config["calls"])
stamp_path = Path(config["stamp"])
trust_path = Path(config["trust"])


def no_station(self):
    return None


fleet_supervisor.StationProcess.ensure = no_station


def code_stamp():
    return stamp_path.read_text(encoding="utf-8").strip()


def checkout_trusted():
    return trust_path.read_text(encoding="utf-8").strip() == "trusted"


def exec_(program, argv):
    """Stand in for os.execv: record the replacement and adopt its services."""
    declared = fleet_supervisor.declared_services(environ) or {}
    services = fleet_supervisor.DeclaredServices(runtime, environ)
    services.reload()
    adopted = {
        name: fleet_supervisor.recorded_service_pid(runtime, name)
        for name in declared
    }
    with calls.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"program": program, "argv": list(argv), "adopted": adopted})
            + "\\n"
        )


raise SystemExit(
    fleet_supervisor.serve(
        environ,
        exec_=exec_,
        code_stamp=code_stamp,
        checkout_trusted=checkout_trusted,
    )
)
'''

SERVICE = """
import pathlib
import sys
import time

starts = pathlib.Path(sys.argv[1])
with starts.open("a", encoding="utf-8") as handle:
    handle.write("start\\n")
time.sleep(600)
"""


class Running:
    """One driven supervisor process and the files a test reads it through."""

    def __init__(self, tmp_path: Path, *, services: dict | None = None, trusted: bool):
        self.runtime = tmp_path / "runtime"
        self.state = tmp_path / "state"
        self.stamp = tmp_path / "stamp"
        self.trust = tmp_path / "trust"
        self.calls = tmp_path / "calls.jsonl"
        self.stamp.write_text("stamp-a\n", encoding="utf-8")
        self.trust.write_text(("trusted" if trusted else "untrusted") + "\n")

        config = tmp_path / "config" / "fleet"
        config.mkdir(parents=True)
        self.state.mkdir()
        (self.state / "migration").mkdir()
        (tmp_path / "shm").mkdir()
        if services is not None:
            (config / "services.json").write_text(json.dumps(services))

        environ = {
            **os.environ,
            "FLEET_RUNTIME_DIR": str(self.runtime),
            "FLEET_STATE_DIR": str(self.state),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "FLEET_SESSION_TMP_ROOT": str(tmp_path / "shm"),
            "FLEET_HEALTH_SAMPLER": "",
            "FLEET_NPROC": str(_open_task_limit()),
            "SLURM_JOB_ID": "",
            "PYTHONPATH": str(ROOT),
        }
        specification = tmp_path / "driver.json"
        specification.write_text(
            json.dumps(
                {
                    "environ": environ,
                    "runtime": str(self.runtime),
                    "calls": str(self.calls),
                    "stamp": str(self.stamp),
                    "trust": str(self.trust),
                }
            )
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

    def set_stamp(self, value: str) -> None:
        """Move the checkout's stamp in one rename, so no read sees it half-written."""
        replacement = self.stamp.with_suffix(".next")
        replacement.write_text(value + "\n", encoding="utf-8")
        replacement.replace(self.stamp)

    def recorded_calls(self) -> list[dict]:
        if not self.calls.exists():
            return []
        return [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def stop(self) -> None:
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


def _open_task_limit() -> int:
    import resource

    soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
    return 6000 if soft == resource.RLIM_INFINITY else soft


def test_a_moved_stamp_re_executes_once_on_a_trusted_checkout(tmp_path):
    run = Running(tmp_path, trusted=True)
    try:
        wait_for((run.runtime / "requests").exists)
        assert run.recorded_calls() == []

        run.set_stamp("stamp-b")
        wait_for(run.recorded_calls)

        # A real exec never returns; the stand-in does, so the loop sees the
        # same moved stamp again on its next passes and must not act on it.
        time.sleep(fleet_supervisor.REQUEST_WAIT_SECONDS * 4)
        calls = run.recorded_calls()
        assert len(calls) == 1, calls
        assert calls[0]["argv"][1:3] == ["-m", "reckon.crew.fleet_supervisor"]
        assert "--standby" not in calls[0]["argv"]
    finally:
        run.stop()


def test_an_untrusted_checkout_leaves_the_supervisor_running(tmp_path):
    run = Running(tmp_path, trusted=False)
    try:
        wait_for((run.runtime / "requests").exists)
        run.set_stamp("stamp-b")
        time.sleep(fleet_supervisor.REQUEST_WAIT_SECONDS * 4)

        assert run.recorded_calls() == []
        # The loop is still serving: a request written now is answered.
        token = "b" * 32
        send_request(run.runtime, f"ready {token}")
        answered = run.state / "migration" / f"ready-{token}.json"
        wait_for(answered.exists)
    finally:
        run.stop()


def test_a_declared_service_keeps_running_across_the_re_exec(tmp_path):
    starts = tmp_path / "starts"
    script = tmp_path / "service.py"
    script.write_text(SERVICE, encoding="utf-8")
    run = Running(
        tmp_path,
        services={"demo": [sys.executable, str(script), str(starts)]},
        trusted=True,
    )
    try:
        wait_for((run.runtime / "requests").exists)
        wait_for(lambda: starts.exists() and starts.read_text().count("start") == 1)
        recorded = fleet_supervisor.recorded_service_pid(run.runtime, "demo")
        assert recorded is not None

        run.set_stamp("stamp-b")
        wait_for(run.recorded_calls)
        time.sleep(fleet_supervisor.REQUEST_WAIT_SECONDS * 4)

        calls = run.recorded_calls()
        assert len(calls) == 1, calls
        # The replacement image finds the service the old image started: same
        # pid, so the recorded service is adopted rather than started again.
        assert calls[0]["adopted"] == {"demo": recorded}
        assert recorded_service_alive(recorded)
        assert starts.read_text().count("start") == 1

        run.stop()
        assert starts.read_text().count("start") == 1
    finally:
        if run.process.poll() is None:
            run.stop()


def recorded_service_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
