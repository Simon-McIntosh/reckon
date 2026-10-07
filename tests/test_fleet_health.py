"""Gate: the health sampler names a stall, and names what stalled.

The sampler writes a summary line every minute and a full process table only
when a threshold is crossed. Two triggers are added here — the node-wide
D-state count and the one-minute load — because the failed kill that drained a
fleet node and the D-state storm two hours later crossed none of the leak
counters the sampler already watched, so neither was attributable. The table
it saves now carries each process's thread count and wait channel, and lists
every D-state process on the node whoever owns it, because a stall is often in
another user's process or a kernel thread.

Every case drives exactly one sampling pass (``--once``) against fixture
readings: a temporary ``/proc``, a temporary cgroup directory, a temporary
state directory and stub ``ps``/``scontrol`` executables on PATH. No case reads
the real ``/proc``; none writes under the real state directory.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "fleet-health"

# The stub process lister. It answers each of the sampler's calls from the
# fixture file named by the environment, so one pass can be composed from
# readings no real node would produce at once.
STUB_PS = """\
#!/bin/bash
args="$*"
if [[ "$args" == *nlwp* ]]; then
    if [[ "$args" == *"user,etimes"* ]]; then
        cat "$FIX_DSTATE"
    else
        cat "$FIX_SNAP"
    fi
elif [[ "$args" == *"args="* ]]; then
    cat "$FIX_ARGS"
elif [[ "$args" == *"stat="* ]]; then
    cat "$FIX_ESTAT"
elif [[ "$args" == *"-L"* ]]; then
    cat "$FIX_THREADS"
elif [[ "$args" == *"ppid=,comm="* ]]; then
    cat "$FIX_TABLE"
fi
"""

STUB_SCONTROL = """\
#!/bin/bash
if [[ "$*" == *ping* ]]; then
    echo "Slurmctld(primary) at slurmctld is UP"
fi
"""

_SNAP_FIXTURE = (
    "    PID    PPID    PGID ELAPSED   RSS NLWP WCHAN        STAT COMMAND   COMMAND\n"
    "      1       0       1     100 10240    5 -            S    systemd   /sbin/init\n"
)
# One D-state kernel thread owned by another user, so the node-wide listing is
# shown to carry a process the calling user does not own.
_DSTATE_FIXTURE = (
    "    PID    PPID    USER ELAPSED NLWP WCHAN            STAT COMMAND   COMMAND\n"
    "   4242       1    root     900    1 rpc_wait_bit_ki  D    nfsd      [nfsd]\n"
)

_SAMPLE_HEADER = (
    "utc\tload1\tload5\tprocs_running\tprocs_blocked\tdstate\tmem_avail_mb"
    "\tswap_free_mb\tjob_mem_mb\tjob_mem_max_mb\thome_stat_ms\tslurmd_ms"
    "\tctld_ping\ttasks\tpython_procs\ttop_parent"
)


class Sandbox:
    def __init__(self, root: Path, env: dict, state: Path) -> None:
        self.root = root
        self.env = env
        self.state = state

    @property
    def health(self) -> Path:
        return self.state / "health"

    def samples(self) -> list[Path]:
        return sorted(self.health.glob("*.tsv"))

    def snapshots(self) -> list[Path]:
        return sorted(self.health.glob("*.ps"))

    def alerts(self) -> str:
        path = self.state / "alerts.log"
        return path.read_text() if path.exists() else ""


def sandbox(
    tmp_path: Path,
    *,
    load1: str = "0.10",
    dstate_procs: int = 0,
    tasks: int = 1,
    python_procs: int = 0,
    mem_avail_kb: int = 60_000_000,
    extra_env: dict | None = None,
) -> Sandbox:
    root = tmp_path
    proc = root / "proc"
    (proc / "self").mkdir(parents=True)
    cg = root / "stub" / "cgroup"
    cg.mkdir(parents=True)
    stub = root / "stubbin"
    stub.mkdir()
    state = root / "state"
    state.mkdir()
    home = root / "home"
    home.mkdir()
    runtime = root / "runtime"
    runtime.mkdir()

    (proc / "loadavg").write_text(f"{load1} 0.20 0.30 1/100 12345\n")
    (proc / "stat").write_text("cpu 1 2 3 4 0 0 2 0\nprocs_running 1\nprocs_blocked 0\n")
    (proc / "meminfo").write_text(f"MemAvailable: {mem_avail_kb} kB\nSwapFree: 1000000 kB\n")
    (proc / "self" / "cgroup").write_text("0::/slurm/uid_1/job_100/step_0/task_0\n")
    (cg / "memory.current").write_text("1048576\n")
    (cg / "memory.max").write_text("max\n")

    (root / "estat").write_text("D\n" * dstate_procs + "S\n")
    (root / "threads").write_text("".join(f"1 thread{i}\n" for i in range(tasks)))
    (root / "table").write_text(
        "".join(f"{i + 1} 1 python{i}\n" for i in range(python_procs)) or "1 1 bash\n"
    )
    (root / "args").write_text("/usr/bin/python worker\n")
    (root / "snap").write_text(_SNAP_FIXTURE)
    (root / "dstate").write_text(_DSTATE_FIXTURE)

    for name, body in (("ps", STUB_PS), ("scontrol", STUB_SCONTROL)):
        exe = stub / name
        exe.write_text(body)
        exe.chmod(0o755)

    env = {
        **os.environ,
        "SLURM_JOB_ID": "100",
        "FLEET_STATE_DIR": str(state),
        "FLEET_PROC_DIR": str(proc),
        "FLEET_CGROUP_DIR": str(cg),
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
        "FIX_ESTAT": str(root / "estat"),
        "FIX_THREADS": str(root / "threads"),
        "FIX_TABLE": str(root / "table"),
        "FIX_ARGS": str(root / "args"),
        "FIX_SNAP": str(root / "snap"),
        "FIX_DSTATE": str(root / "dstate"),
    }
    if extra_env:
        env.update(extra_env)
    return Sandbox(root, env, state)


def run_pass(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), "--once"], env=env, capture_output=True, text=True)


def run_passes(env: dict, count: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--passes", str(count)],
        env=env,
        capture_output=True,
        text=True,
    )


# Thresholds pinned in each case so a case states the boundary it drives rather
# than relying on a default that may move.
BASE = {
    "FLEET_TASKS_ALERT": "4000",
    "FLEET_PYTHON_ALERT": "1000",
    "FLEET_MEM_AVAIL_ALERT_MB": "12000",
    "FLEET_DSTATE_ALERT": "50",
    "FLEET_LOAD_ALERT": "56",
}


def test_one_sample_line_is_written_and_below_every_threshold_writes_no_snapshot(
    tmp_path: Path,
) -> None:
    box = sandbox(tmp_path, extra_env=BASE)
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    assert box.samples(), "a summary sample is written on every pass"
    assert box.snapshots() == [], "a pass below every threshold writes no process table"
    assert box.alerts() == ""


def test_dstate_trigger_writes_a_snapshot_with_thread_and_wait_columns(
    tmp_path: Path,
) -> None:
    # 60 node-wide D-state tasks against a threshold of 50; every other reading
    # is below its threshold, so the D-state count is the only thing firing.
    box = sandbox(tmp_path, dstate_procs=60, extra_env=BASE)
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr

    snaps = box.snapshots()
    assert len(snaps) == 1
    text = snaps[0].read_text()
    assert "NLWP" in text and "WCHAN" in text, "the table carries thread count and wait channel"
    assert "nfsd" in text, "the D-state listing carries a process another user owns"
    assert "root" in text

    # The sample line reports the same count that fired the trigger.
    line = box.samples()[0].read_text().splitlines()
    assert line[0] == _SAMPLE_HEADER
    assert line[-1].split("\t")[5] == "60"

    alert = box.alerts()
    assert "under pressure" in alert
    assert "D-state" in alert


def test_load_trigger_writes_a_snapshot_with_thread_and_wait_columns(
    tmp_path: Path,
) -> None:
    box = sandbox(tmp_path, load1="90.0", extra_env=BASE)
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr

    snaps = box.snapshots()
    assert len(snaps) == 1
    text = snaps[0].read_text()
    assert "NLWP" in text and "WCHAN" in text
    assert "load 90.0" in box.alerts()


def test_dstate_threshold_is_overridable(tmp_path: Path) -> None:
    # Three D-state tasks fire only when the threshold is lowered by environment.
    box = sandbox(tmp_path, dstate_procs=3, extra_env={**BASE, "FLEET_DSTATE_ALERT": "2"})
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    assert len(box.snapshots()) == 1


def test_load_threshold_is_overridable(tmp_path: Path) -> None:
    box = sandbox(tmp_path, load1="1.5", extra_env={**BASE, "FLEET_LOAD_ALERT": "1.0"})
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr
    assert len(box.snapshots()) == 1


@pytest.mark.parametrize(
    ("kwargs", "marker"),
    [
        ({"tasks": 4001}, "4001 tasks"),
        ({"python_procs": 1001}, "1001 python processes"),
        ({"mem_avail_kb": 10240}, "10 MB available"),
    ],
)
def test_existing_triggers_still_fire(tmp_path: Path, kwargs: dict, marker: str) -> None:
    box = sandbox(tmp_path, extra_env=BASE, **kwargs)
    result = run_pass(box.env)
    assert result.returncode == 0, result.stderr

    snaps = box.snapshots()
    assert len(snaps) == 1
    text = snaps[0].read_text()
    assert "NLWP" in text and "WCHAN" in text
    assert marker in box.alerts()


def test_two_crossing_passes_within_the_bound_write_one_snapshot(tmp_path: Path) -> None:
    # Two passes each cross the D-state threshold two seconds apart, inside a
    # three-second bound, so only the first writes a snapshot.
    box = sandbox(
        tmp_path,
        dstate_procs=60,
        extra_env={**BASE, "FLEET_SNAPSHOT_INTERVAL": "3", "FLEET_SLEEP_SECONDS": "2"},
    )
    result = run_passes(box.env, 2)
    assert result.returncode == 0, result.stderr
    assert len(box.samples()[0].read_text().splitlines()) >= 3, "both passes wrote a sample line"
    assert len(box.snapshots()) == 1
    assert len(box.alerts().splitlines()) == 1


def test_a_pass_after_the_bound_writes_a_second_snapshot(tmp_path: Path) -> None:
    # Three passes two seconds apart against a three-second bound: the second
    # is inside it and writes nothing, the third is past it and writes again.
    box = sandbox(
        tmp_path,
        dstate_procs=60,
        extra_env={**BASE, "FLEET_SNAPSHOT_INTERVAL": "3", "FLEET_SLEEP_SECONDS": "2"},
    )
    result = run_passes(box.env, 3)
    assert result.returncode == 0, result.stderr
    assert len(box.snapshots()) == 2
    assert len(box.alerts().splitlines()) == 2