"""Allocation lifetime is independent of a replaceable Python supervisor."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from reckon.crew import fleet_node, fleet_supervisor

ROOT = Path(__file__).resolve().parents[1]


def _hold(home):
    return fleet_node.generate_hold_script(
        fleet_node.fleet_size({}),
        log_path=home / "batch.log",
        environ={"PATH": os.environ["PATH"]},
    )


def _eventually(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"condition did not become true within {seconds}s")


def _home(tmp_path, command):
    home = tmp_path / "home"
    executable = home / ".local/bin/fleet-supervisor"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/bash\n" + command + "\n")
    executable.chmod(0o755)
    return home


def _launch(home, script):
    env = dict(os.environ, HOME=str(home), TMPDIR=str(home.parent))
    log = (home / "holder.log").open("w")
    process = subprocess.Popen(
        ["bash", "-c", script], env=env, stdout=log, stderr=log, start_new_session=True
    )
    return process, log


def _finish(process, log):
    if process.poll() is None:
        # This group contains only the shell and fixtures this test launched.
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    log.close()


def test_a_clean_unrequested_exit_is_restarted(tmp_path):
    home = _home(tmp_path, 'echo "$BASHPID" >> "$HOME/starts"; exit 0')
    process, log = _launch(home, _hold(home))
    try:
        starts = home / "starts"
        _eventually(starts.exists)
        assert process.poll() is None, (
            "a clean supervisor exit must leave the holder alive"
        )
        _eventually(lambda: len(starts.read_text().splitlines()) >= 2, seconds=9)
    finally:
        _finish(process, log)


def test_stop_marks_retirement_but_reload_does_not(tmp_path):
    lifetime = tmp_path / "lifetime"
    lifetime.mkdir()
    env = {"FLEET_LIFETIME_DIRECTORY": str(lifetime)}
    executions = []
    assert fleet_supervisor.handle_line(
        "reload", tmp_path, env, lambda *args: executions.append(args)
    )
    assert executions
    assert not (lifetime / "stop").exists()
    assert fleet_supervisor.handle_line("stop", tmp_path, env) is False
    assert (lifetime / "stop").is_file()


def test_deliberate_retirement_exits_the_holder(tmp_path):
    home = _home(tmp_path, ': > "$FLEET_LIFETIME_DIRECTORY/stop"')
    process, log = _launch(home, _hold(home))
    try:
        assert process.wait(timeout=5) == 0
        assert "restart in" not in (home / "holder.log").read_text()
    finally:
        _finish(process, log)


def test_promoted_standby_stays_active_across_a_crash(tmp_path):
    home = _home(
        tmp_path,
        """echo "$*" >> "$HOME/modes"
if [ ! -f "$FLEET_LIFETIME_DIRECTORY/active" ]; then
    : > "$FLEET_LIFETIME_DIRECTORY/active"
else
    : > "$FLEET_LIFETIME_DIRECTORY/stop"
fi""",
    )
    script = fleet_node.generate_hold_script(
        fleet_node.fleet_size({}),
        log_path=home / "batch.log",
        standby=True,
        environ={"PATH": os.environ["PATH"]},
    )
    process, log = _launch(home, script)
    try:
        assert process.wait(timeout=10) == 0
        assert (home / "modes").read_text().splitlines() == ["--standby", ""]
    finally:
        _finish(process, log)


def test_the_holder_uses_the_service_backoff(monkeypatch, tmp_path):
    transition = fleet_supervisor.next_backoff

    def capped(delay, ran_seconds):
        wait, following = transition(delay, ran_seconds)
        return min(wait, 13.0), min(following, 13.0)

    monkeypatch.setattr(fleet_supervisor, "next_backoff", capped)
    home = _home(tmp_path, "exit 1")
    sleep = home / ".local/bin/sleep"
    sleep.write_text("""#!/bin/bash
echo "$1" >> "$HOME/delays"
if [ "$(wc -l < "$HOME/delays")" -ge 4 ]; then
    : > "$FLEET_LIFETIME_DIRECTORY/stop"
fi
""")
    sleep.chmod(0o755)
    monkeypatch.setenv("PATH", str(sleep.parent) + os.pathsep + os.environ["PATH"])
    process, log = _launch(home, _hold(home))
    try:
        assert process.wait(timeout=5) == 0
        assert [
            float(value) for value in (home / "delays").read_text().splitlines()
        ] == [5, 10, 13, 13]
    finally:
        _finish(process, log)


@pytest.mark.timeout(550)
@pytest.mark.skipif(
    os.environ.get("RECKON_FLEET_REHEARSAL") != "1",
    reason="requires an explicit all_debug scheduler rehearsal",
)
def test_sessions_survive_a_killed_supervisor_and_a_broken_interpreter(tmp_path):
    """The same scheduler assertions reject a Python-owned batch process."""
    receipts = Path(os.environ["RECKON_FLEET_RECEIPTS"])
    receipts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fleet-rehearsal-", dir=receipts) as shared:
        work = Path(shared)
        interpreter = work / "python"
        shutil.copyfile(sys.executable, interpreter)
        interpreter.chmod(0o755)
        supervisor = work / "supervisor.py"
        supervisor.write_text(REHEARSAL_SUPERVISOR)
        home = _home(
            work, f"exec {shlex.quote(str(interpreter))} {shlex.quote(str(supervisor))}"
        )
        hold = _hold(home)
        if os.environ.get("RECKON_FLEET_BATCH_PROCESS") == "supervisor":
            hold = '#!/bin/bash\nexec "$HOME/.local/bin/fleet-supervisor"\n'
        hold_path = work / "hold.sh"
        hold_path.write_text(hold)
        batch = work / "batch.sh"
        batch.write_text(
            "#!/bin/bash\nset -eu\n"
            "export TMPDIR=/tmp\n"
            f"export HOME={shlex.quote(str(home))} PYTHONHOME={shlex.quote(sys.base_prefix)}\n"
            f"export REHEARSAL_DIRECTORY={shlex.quote(str(work))}\n"
            f"export PYTHONPATH={shlex.quote(str(ROOT))}\n"
            f"export REHEARSAL_MONITOR_PYTHON={shlex.quote(sys.executable)}\n"
            f"exec bash {shlex.quote(str(hold_path))}\n"
        )
        submit = subprocess.run(
            [
                "/usr/bin/sbatch",
                "--parsable",
                "--partition=all_debug",
                "--account=iter",
                "--time=00:08:00",
                "--nodes=1",
                "--ntasks=2",
                "--cpus-per-task=1",
                "--mem=2G",
                f"--output={receipts / 'rehearsal-%j.log'}",
                str(batch),
            ],
            capture_output=True,
            text=True,
            check=True,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in {"SLURM_JOB_ID", "SLURM_JOBID"}
            },
        )
        job = submit.stdout.strip().split(";")[0]
        assert job.isdigit(), submit.stdout
        receipt = {
            "job_id": job,
            "control": os.environ.get("RECKON_FLEET_BATCH_PROCESS") == "supervisor",
        }
        receipt_path = receipts / f"receipt-{job}.json"

        def query(*args):
            result = subprocess.run(
                ["/usr/bin/" + args[0], *args[1:]],
                capture_output=True,
                text=True,
                check=False,
            )
            if args[0] == "squeue" and "Invalid job id" in result.stderr:
                return ""
            result.check_returncode()
            return result.stdout.strip()

        try:
            first = _eventually(lambda: _read_json(work / "ready.json"), seconds=120)
            receipt["first"] = first
            receipt["before_queue"] = query("squeue", "-h", "-j", job, "-o", "%T")
            assert receipt["before_queue"] == "RUNNING", receipt
            # The target pid belongs to this rehearsal job; its own killer runs there.
            (work / "kill").touch()
            ended = _eventually((work / "killed").exists, seconds=10)
            assert ended
            killed_at = time.monotonic()
            receipt["ended_within_ten_seconds"] = False
            while time.monotonic() - killed_at < 10:
                if query("squeue", "-h", "-j", job, "-o", "%T") not in {
                    "RUNNING",
                    "COMPLETING",
                }:
                    receipt["ended_within_ten_seconds"] = True
                    receipt["end_seconds"] = time.monotonic() - killed_at
                    break
                time.sleep(0.2)
            time.sleep(max(0, 30 - (time.monotonic() - killed_at)))
            receipt["thirty_second_queue"] = query(
                "squeue", "-h", "-j", job, "-o", "%T"
            )
            receipt["thirty_second_steps"] = (
                query("scontrol", "show", "step", f"{job}.0", "-o")
                if receipt["thirty_second_queue"] == "RUNNING"
                else ""
            )
            receipt["thirty_second_processes"] = _read_json(work / "survivors.json")
            receipt["starts"] = (work / "starts").read_text().splitlines()
            receipt_path.write_text(json.dumps(receipt, indent=2))
            assert receipt["thirty_second_queue"] == "RUNNING", receipt
            assert receipt["thirty_second_processes"] == {
                "descendant": True,
                "step": True,
            }, receipt
            assert "State=RUNNING" in receipt["thirty_second_steps"], receipt
            assert len(receipt["starts"]) >= 2, receipt
            second = _read_json(work / "current.json")
            assert second["descendant"] == first["descendant"]
            assert second["step"] == first["step"]
            assert float(receipt["starts"][1]) - float(receipt["starts"][0]) < 10
            # Only the decoy copy is replaced; the account's interpreter is untouched.
            interpreter.unlink()
            interpreter.write_text("broken interpreter\n")
            interpreter.chmod(0o755)
            before = len(receipt["starts"])
            attempts_before = (
                (receipts / f"rehearsal-{job}.log")
                .read_text()
                .count("fleet supervisor starting")
            )
            (work / "break").touch()
            time.sleep(61)
            batch_log = (receipts / f"rehearsal-{job}.log").read_text()
            receipt["broken_restart_count"] = (
                batch_log.count("fleet supervisor starting") - attempts_before
            )
            receipt["broken_queue"] = query("squeue", "-h", "-j", job, "-o", "%T")
            receipt_path.write_text(json.dumps(receipt, indent=2))
            assert 1 <= receipt["broken_restart_count"] <= 3, receipt
            assert receipt["broken_queue"] == "RUNNING", receipt
            assert len((work / "starts").read_text().splitlines()) == before
            # The lifetime marker is node-local, so retirement is requested there.
            (work / "retire").touch()
            _eventually(
                lambda: not query("squeue", "-h", "-j", job, "-o", "%T"), seconds=100
            )
        finally:
            state = query("squeue", "-h", "-j", job, "-o", "%T")
            if state and state != "COMPLETING":
                print(
                    f"Retiring rehearsal job {job}; other allocations are outside this test"
                )
                subprocess.run(["/usr/bin/scancel", job], check=True)
            receipt["accounting"] = query(
                "sacct", "-n", "-j", job, "--format=JobID,State,ExitCode", "-P"
            )
            receipt_path.write_text(json.dumps(receipt, indent=2))


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


REHEARSAL_SUPERVISOR = r'''
import json, os, signal, subprocess, time
from pathlib import Path
work = Path(os.environ["REHEARSAL_DIRECTORY"])
with (work / "starts").open("a") as log:
    log.write(str(time.monotonic()) + "\n")
(work / "lifetime").write_text(os.environ.get("FLEET_LIFETIME_DIRECTORY", ""))
ready = work / "ready.json"
if not ready.exists():
    descendant = subprocess.Popen(["sleep", "400"], start_new_session=True)
    step = subprocess.Popen(["/usr/bin/srun", "--overlap", "--cpu-bind=none", "--ntasks=1", "--cpus-per-task=1", "--mem=256M", "bash", "-c", 'echo "$BASHPID" > "$REHEARSAL_DIRECTORY/step.pid"; exec sleep 400'], start_new_session=True)
    deadline = time.monotonic() + 30
    while not (work / "step.pid").exists():
        assert time.monotonic() < deadline
        time.sleep(.1)
    data = {"supervisor": os.getpid(), "descendant": descendant.pid, "step": int((work / "step.pid").read_text())}
    ready.write_text(json.dumps(data))
    monitor = work / "monitor.py"
    monitor.write_text(r"""
import json, os, signal, time
from pathlib import Path
work = Path(os.environ['REHEARSAL_DIRECTORY'])
first = json.loads((work / 'ready.json').read_text())
while not (work / 'kill').exists(): time.sleep(.1)
(work / 'killed').touch()
os.kill(first['supervisor'], signal.SIGKILL)
time.sleep(29)
def alive(pid):
    try: os.kill(pid, 0); return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except ProcessLookupError: return False
(work / 'survivors.json').write_text(json.dumps({name: alive(first[name]) for name in ('descendant', 'step')}))
while not (work / 'break').exists(): time.sleep(.1)
current = json.loads((work / 'current.json').read_text())
os.kill(current['supervisor'], signal.SIGKILL)
while not (work / 'retire').exists(): time.sleep(.1)
Path(os.environ['FLEET_LIFETIME_DIRECTORY'], 'stop').touch()
""")
    subprocess.Popen([os.environ['REHEARSAL_MONITOR_PYTHON'], str(monitor)], start_new_session=True)
else:
    data = json.loads(ready.read_text())
data['supervisor'] = os.getpid()
(work / "current.json").write_text(json.dumps(data))
while True: time.sleep(1)
'''
