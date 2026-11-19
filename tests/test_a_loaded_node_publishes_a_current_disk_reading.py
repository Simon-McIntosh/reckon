"""Gate: a loaded node still publishes a current disk reading.

Measured on 19 November: on a node at load 57 every session carried a
``node-disk-stale`` row, because a pass published its reading only after a
60-second wait and seven steps allowed 20 seconds each, and the scratch sweep
that ran *inside* the reading step took longer than the step's own bound. Two
passes then separated consecutive readings by well over the reader's 180-second
freshness limit, and the row that reported it named no process, so no session
could act on it or clear it.

Every case here drives the real ``bin/fleet-health`` script against a fixture
``/proc``, a fixture cgroup, a stub ``/proc`` reader set and a temporary state
directory. The seven steps and the wait are scaled together — a 20-second bound
becomes 2, the 60-second wait becomes 3, and the reader's 180-second limit
becomes 8 — so the arithmetic under test is the sampler's own, and the limit the
test measures against is read back from the reader rather than written down
here.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest

from reckon._timestamps import parse_utc
from reckon.crew import fleet_supervisor as supervisor
from reckon.hooks import coordinator_obligations as hooks
from tests.test_fleet_health import BASE, sandbox

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "fleet-health"
INTERPRETER = REPO_ROOT / ".venv" / "bin" / "python"

# The scale: every step is allowed 2 seconds, a pass is allowed 4, the wait
# between passes is 2, and the reader's limit is 8. Real values are 20, 90, 60
# and 180. The steps are allowed more between them than the pass is, which is
# the case that decides the measure: a pass that let each step have its own
# bound would separate two readings by the whole sum.
STEP_BOUND = 2
STEP_SLEEP = 1.8
PASS_BOUND = 4
WAIT = 2
LIMIT = 8

STUB_PS = """\
#!/bin/bash
args="$*"
if [[ "$args" == *"-L"* || "$args" == *"pid=,ppid=,comm="* ]]; then
    sleep "$FIX_STEP_SLEEP"
fi
if [[ "$args" == *"stat="* ]]; then
    cat "$FIX_ESTAT"
elif [[ "$args" == *"-L"* ]]; then
    cat "$FIX_THREADS"
elif [[ "$args" == *"ppid=,comm="* ]]; then
    cat "$FIX_TABLE"
elif [[ "$args" == *"args="* ]]; then
    cat "$FIX_ARGS"
fi
exit 0
"""

STUB_SCONTROL = """\
#!/bin/bash
sleep "$FIX_STEP_SLEEP"
if [[ "$*" == *ping* ]]; then
    echo "Slurmctld(primary) at slurmctld is UP"
fi
"""

STUB_STAT = """\
#!/bin/bash
sleep "$FIX_STEP_SLEEP"
exec /usr/bin/stat "$@"
"""

STUB_SHIM = """\
#!/bin/bash
sleep "$FIX_STEP_SLEEP"
echo 0
"""


def reading_now(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) and document.get("utc") else None


class Publications:
    """Every distinct reading a sampler published while this watched.

    The record is rewritten by the pass's sweep step as well as published by
    its reading step, so readings are told apart by the instant they carry
    rather than by the file changing.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.readings: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def _take(self) -> None:
        document = reading_now(self.path)
        if document is None:
            return
        if self.readings and self.readings[-1]["utc"] == document["utc"]:
            return
        self.readings.append(document)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._take()
            time.sleep(0.05)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._stop.set()
        self._thread.join(timeout=5)
        self._take()
        return False

    def moments(self) -> list[float]:
        return [parse_utc(reading["utc"]).timestamp() for reading in self.readings]

    def separations(self) -> list[float]:
        moments = self.moments()
        return [later - earlier for earlier, later in itertools.pairwise(moments)]


def wait_for(condition, *, deadline: float = 120.0, note: str = "") -> None:
    timeout = time.monotonic() + deadline
    while time.monotonic() < timeout:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {note or condition}")


def group_is_live(pid: int) -> bool:
    """Whether any process in this group is still running rather than reaped."""
    shown = subprocess.run(
        ["ps", "-o", "stat=", "-g", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return any(state and not state.startswith("Z") for state in shown.stdout.split())


def stop_sampler(process: subprocess.Popen) -> None:
    """End a sampler and the steps it started, which share its process group.

    The sampler holds its exclusive lock through a descriptor its children
    inherit, so a replacement can only take over once nothing in the tree is
    still running. The leader's own zombie keeps the group listed until it is
    reaped, which is why the wait asks what is running rather than whether any
    member remains.
    """
    for signature in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signature)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not group_is_live(process.pid):
                break
            time.sleep(0.05)
        else:
            continue
        break
    process.wait(timeout=30)


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A fixture node whose sampler runs at the scaled bounds declared above."""
    root = tmp_path / "node"
    root.mkdir()
    mount = root / "mount"
    mount.mkdir()
    flight = tmp_path / "flight.yaml"
    flight.write_text(
        "worktree:\n"
        "  node_tmp_reserve_bytes: 0\n"
        f"  node_tmp_sample_max_age_seconds: {LIMIT}\n"
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(flight))
    box = sandbox(
        root,
        extra_env={
            **BASE,
            "FLEET_STEP_BOUND_SECONDS": str(STEP_BOUND),
            "FLEET_DISK_BOUND_SECONDS": str(STEP_BOUND),
            "FLEET_SLEEP_SECONDS": str(WAIT),
            "FLEET_PASS_BOUND_SECONDS": str(PASS_BOUND),
            "FLEET_NODE_TMP_ROOT": str(mount),
            "FLEET_DISK_PYTHON": str(INTERPRETER),
            "RECKON_FLIGHT_CONFIG": str(flight),
            "RECKON_HOME": str(root / "crew-home"),
            "RECKON_WORKER_SCRATCH_ROOT": str(root / "scratch"),
            "FIX_STEP_SLEEP": str(STEP_SLEEP),
        },
    )
    stubs = root / "stubbin"
    for name, body in (
        ("ps", STUB_PS),
        ("scontrol", STUB_SCONTROL),
        ("stat", STUB_STAT),
        ("permission-check", STUB_SHIM),
    ):
        stub = stubs / name
        stub.write_text(body)
        stub.chmod(0o755)
    # The fleet the reader resolves, so a reading the sampler publishes is a
    # reading the reader accepts rather than a file it refuses for another job.
    (box.state / supervisor.RECORD_NAME).write_text(
        json.dumps({"node": socket.gethostname().split(".")[0], "job_id": "100"})
    )
    return box, mount


def test_consecutive_readings_stay_inside_the_readers_limit(node):
    box, mount = node
    limit = supervisor.node_disk_sample_max_age_seconds()
    assert limit == LIMIT, "the reader's limit is the one this case measures against"
    record = box.state / supervisor.NODE_DISK_RECORD
    with Publications(record) as published:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--passes", "2"],
            env=box.env,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    # Every step of a pass is a stub that answers only after its whole bound,
    # so a pass that let each step have its own bound would cost more than its
    # own; the reading it publishes is at its head either way.
    assert len(published.readings) >= 2, (
        "the pass published no second reading to compare: "
        f"{[reading.get('utc') for reading in published.readings]}"
    )
    gaps = published.separations()
    assert gaps and max(gaps) < limit, (
        f"consecutive published readings are {max(gaps):.1f} seconds apart, "
        f"beyond the {limit}-second limit the reader applies"
    )
    # The reading this measured is the one the reader reads.
    accepted = supervisor.published_node_disk_sample(box.env)
    assert accepted is not None
    assert accepted["utc"] == published.readings[-1]["utc"]
    assert accepted["mount"] == str(mount.resolve())


def test_a_sampler_replaced_mid_wait_keeps_publishing_inside_the_limit(node):
    box, _mount = node
    limit = supervisor.node_disk_sample_max_age_seconds()
    record = box.state / supervisor.NODE_DISK_RECORD
    first = subprocess.Popen(
        ["bash", str(SCRIPT), "--passes", "3"],
        env=box.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for(
            lambda: reading_now(record) is not None,
            note="the first sampler's reading",
        )
        published = reading_now(record)
        assert published["sampler_pid"] == first.pid
        # The row is appended after the pass's last step, so its appearance
        # means the pass is over and the sampler is in its wait.
        wait_for(
            lambda: list(box.health.glob("*.tsv")),
            note="the first sampler's row",
        )
        first_published_at = parse_utc(published["utc"]).timestamp()
    finally:
        stop_sampler(first)
    second = subprocess.Popen(
        ["bash", str(SCRIPT), "--once"],
        env=box.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        wait_for(
            lambda: (reading_now(record) or {}).get("sampler_pid") == second.pid,
            note="the replacement sampler's reading",
        )
        replacement = reading_now(record)
    finally:
        stop_sampler(second)
    separation = parse_utc(replacement["utc"]).timestamp() - first_published_at
    assert separation < limit, (
        f"the replacement sampler published {separation:.1f} seconds after its "
        f"predecessor, beyond the {limit}-second limit the reader applies"
    )


def test_an_expired_reading_names_its_sampler_and_holds_no_stop_hook(node):
    box, mount = node
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        expired = supervisor.node_disk_snapshot(mount, reserve_bytes=0)
        expired["utc"] = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        expired["job_id"] = "100"
        expired["sampler_pid"] = holder.pid
        (box.state / supervisor.NODE_DISK_RECORD).write_text(json.dumps(expired))
        environ = {"FLEET_STATE_DIR": str(box.state)}
        with pytest.raises(supervisor.NodeDiskSampleError) as refused:
            supervisor.published_node_disk_sample(environ)
        row = supervisor.node_disk_reading_obligation(refused.value)
    finally:
        holder.terminate()
        holder.wait(timeout=30)
    assert row["kind"] == "node-disk-stale"
    assert f"sampler pid {holder.pid}" in row["reason"], row["reason"]
    assert "400" in row["reason"] and "seconds old" in row["reason"]
    assert 399 <= row["age_seconds"] <= 402
    # Nothing a session owns can clear this row, so it is listed and never
    # holds a turn open. The low-space row, which a session can act on, still
    # does — the predicate is shown seeing something known present.
    assert hooks._blocking_items([row]) == []
    pressure = supervisor.node_disk_pressure(
        supervisor.node_disk_snapshot(mount, reserve_bytes=10**18)
    )
    assert pressure["kind"] == "node-disk-reserve"
    assert hooks._blocking_items([pressure]) == [pressure]
