"""A running fleet supervisor replaces its image when the checkout moves.

The supervisor outlives every session on the node, so a fix to the code it runs
reaches the node only when it notices that the checkout has moved on. It compares
its recorded code stamp with the checkout's on each pass and re-executes in place
through the same reload path a manual request takes -- unless the checkout is
mid-edit, in which case it keeps running the image it has. The trust test behind
that verdict is expensive, so it is paid for once per moved stamp, not once per
pass.
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
# things a test must control enter by injection: the process replacement, the
# cheap code stamp, the moved-checkout verdict and the clock.
DRIVER = """
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from reckon.crew import fleet_supervisor

config = json.loads(Path(sys.argv[2]).read_text())
environ = dict(config["environ"])
runtime = Path(config["runtime"])
calls = Path(config["calls"])
verdicts = Path(config["verdicts"])
stamp_path = Path(config["stamp"])
trust_path = Path(config["trust"])
clock_path = Path(config["clock"])


def no_station(self):
    return None


fleet_supervisor.StationProcess.ensure = no_station


def code_stamp():
    return stamp_path.read_text(encoding="utf-8").strip()


def clock():
    return float(clock_path.read_text(encoding="utf-8").strip())


def moved_checkout(running_stamp):
    current = code_stamp()
    trusted = trust_path.read_text(encoding="utf-8").strip() == "trusted"
    with verdicts.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"running": running_stamp, "disk": current}) + "\\n")
    return types.SimpleNamespace(
        moved=current != running_stamp,
        trusted=trusted,
        detail="",
        running_stamp=running_stamp,
        disk_stamp=current,
    )


def exec_(program, argv):
    declared = fleet_supervisor.declared_services(environ) or {}
    services = fleet_supervisor.DeclaredServices(runtime, environ)
    services.reload()
    adopted = {
        name: fleet_supervisor.recorded_service_pid(runtime, name) for name in declared
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
        moved_checkout=moved_checkout,
        clock=clock,
    )
)
"""

SERVICE = """
import os
import pathlib
import sys
import time

starts = pathlib.Path(sys.argv[1])
with starts.open("a", encoding="utf-8") as handle:
    handle.write(f"start {os.getpid()}\\n")
time.sleep(600)
"""


class Running:
    """One driven supervisor process and the files a test reads it through."""

    def __init__(self, tmp_path: Path, *, services: dict | None = None, trusted: bool):
        self.runtime = tmp_path / "runtime"
        self.state = tmp_path / "state"
        self.stamp = tmp_path / "stamp"
        self.trust = tmp_path / "trust"
        self.clock = tmp_path / "clock"
        self.calls = tmp_path / "calls.jsonl"
        self.verdicts = tmp_path / "verdicts.jsonl"
        self.stamp.write_text("stamp-a\n", encoding="utf-8")
        self.trust.write_text(("trusted" if trusted else "untrusted") + "\n")
        self.clock.write_text("0\n", encoding="utf-8")

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
                    "verdicts": str(self.verdicts),
                    "stamp": str(self.stamp),
                    "trust": str(self.trust),
                    "clock": str(self.clock),
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

    def set_clock(self, seconds: float) -> None:
        self.clock.write_text(f"{seconds}\n", encoding="utf-8")

    def recorded(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def replacements(self) -> list[dict]:
        return self.recorded(self.calls)

    def verdict_checks(self) -> list[dict]:
        return self.recorded(self.verdicts)

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


def _passes(seconds: float) -> None:
    """Let the loop run a known number of its own passes."""
    time.sleep(fleet_supervisor.REQUEST_WAIT_SECONDS * seconds * 2)


def test_a_moved_stamp_re_executes_once_on_a_trusted_checkout(tmp_path):
    run = Running(tmp_path, trusted=True)
    try:
        wait_for((run.runtime / "requests").exists)
        assert run.replacements() == []

        run.set_stamp("stamp-b")
        wait_for(run.replacements)

        # A real exec never returns; the stand-in does, so the loop sees the
        # same moved stamp again on its next passes and must not act on it.
        _passes(2)
        replacements = run.replacements()
        assert len(replacements) == 1, replacements
        assert replacements[0]["argv"][1:3] == ["-m", "reckon.crew.fleet_supervisor"]
        assert "--standby" not in replacements[0]["argv"]
    finally:
        run.stop()


def test_an_untrusted_checkout_leaves_the_supervisor_running(tmp_path):
    run = Running(tmp_path, trusted=False)
    try:
        wait_for((run.runtime / "requests").exists)
        run.set_stamp("stamp-b")
        _passes(2)

        assert run.replacements() == []
        # The loop is still serving: a request written now is answered.
        token = "b" * 32
        send_request(run.runtime, f"ready {token}")
        answered = run.state / "migration" / f"ready-{token}.json"
        wait_for(answered.exists)
    finally:
        run.stop()


def test_a_dirty_checkout_costs_one_trust_test_per_moved_stamp(tmp_path):
    run = Running(tmp_path, trusted=False)
    try:
        wait_for((run.runtime / "requests").exists)
        assert run.verdict_checks() == []

        # One move, then many passes on the tree it moved to: the verdict is
        # remembered for that stamp, so exactly one trust test is paid.
        run.set_stamp("stamp-b")
        wait_for(run.verdict_checks)
        _passes(2)
        checks = run.verdict_checks()
        assert len(checks) == 1, checks
        assert checks[0] == {"running": "stamp-a", "disk": "stamp-b"}
        assert run.replacements() == []

        # A second move is a second stamp, so it costs one more check -- once
        # the interval has passed; inside it the loop does not pay again.
        run.set_stamp("stamp-c")
        _passes(2)
        assert len(run.verdict_checks()) == 1, run.verdict_checks()

        run.set_clock(fleet_supervisor.MOVE_CHECK_INTERVAL_SECONDS)
        wait_for(lambda: len(run.verdict_checks()) == 2)
        _passes(2)
        assert len(run.verdict_checks()) == 2, run.verdict_checks()
        assert run.verdict_checks()[1] == {"running": "stamp-a", "disk": "stamp-c"}
        assert run.replacements() == []
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
        wait_for(lambda: starts.exists() and len(starts.read_text().splitlines()) == 1)
        # The service names its own pid: every later reading is of the process,
        # not of the supervisor's record of it.
        pid = int(starts.read_text().split()[1])

        run.set_stamp("stamp-b")
        wait_for(run.replacements)
        _passes(2)

        assert len(run.replacements()) == 1, run.replacements()
        assert _alive(pid)
        assert fleet_supervisor.lock_held("demo", run.runtime)
        assert len(starts.read_text().splitlines()) == 1, starts.read_text()

        run.stop()
        assert len(starts.read_text().splitlines()) == 1, starts.read_text()
    finally:
        if run.process.poll() is None:
            run.stop()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
