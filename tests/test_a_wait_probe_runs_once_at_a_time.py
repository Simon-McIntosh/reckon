"""A wait's probe is run once, shared, and stopped when it overruns.

Every reader that polled a waiting run ran that run's probe for itself. On
22 November 2026 nine copies of one probe ran at once against a 1.16 GB store,
a new one every few seconds, and every promotion on the node failed on the
database lock it held. The gate in :mod:`reckon.crew.recovery_wait` puts every
reader of one wait behind one run: a result is shared for the interval, a run
in flight is not started again, and a run past its bound is killed and raised
to the run's coordinator.

The figures the gate carries are thirty seconds and sixty seconds; the arms
below pin both. The interval arm compresses the interval so the test does not
spend thirty seconds proving a comparison, and the bound arm uses the real
sixty so the kill is the one the gate actually performs.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import recovery_wait

PROBE_CODE = """
import os
import sys
import time

marker, pid_file, sleep_for = sys.argv[1], sys.argv[2], float(sys.argv[3])
with open(marker, "a", encoding="utf-8") as handle:
    handle.write("run\\n")
with open(pid_file, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
time.sleep(sleep_for)
print("pending")
"""


@pytest.fixture(autouse=True)
def _an_isolated_crew_home(tmp_path, monkeypatch):
    """Point every shared path at a temporary home, and prove it stayed there."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    real = Path.home() / ".config" / "reckon" / "crew" / "wait-probes"
    before = set(real.glob("*")) if real.is_dir() else set()
    yield home
    after = set(real.glob("*")) if real.is_dir() else set()
    assert after == before, "a case wrote into the workstation's own probe results"


def _probe(tmp_path: Path, sleep_for: float, *, name: str = "probe") -> dict:
    marker = tmp_path / f"{name}.marker"
    pid_file = tmp_path / f"{name}.pid"
    vector = [
        shutil.which("python3") or "python3",
        "-c",
        PROBE_CODE,
        str(marker),
        str(tmp_path / f"{name}.pid"),
        str(sleep_for),
    ]
    return {
        "record": {"run_id": f"r-{name}", "worktree": str(tmp_path)},
        "wait": {"probe": vector},
        "marker": marker,
        "pid_file": pid_file,
    }


def _runs(marker: Path) -> int:
    if not marker.exists():
        return 0
    return len(marker.read_text(encoding="utf-8").splitlines())


def _wait_for_marker(marker: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _runs(marker):
            return
        time.sleep(0.02)
    raise AssertionError("the probe never started")


def test_the_gate_carries_the_interval_and_the_bound_the_plan_names() -> None:
    assert recovery_wait.WAIT_PROBE_INTERVAL_SECONDS == 30.0
    assert recovery_wait.WAIT_PROBE_RUN_BOUND_SECONDS == 60.0


def test_ten_readers_polling_one_wait_start_the_probe_once(tmp_path) -> None:
    probe = _probe(tmp_path, 0.0)
    results: list[dict] = []
    start = threading.Barrier(10)

    def read() -> None:
        start.wait()
        results.append(recovery_wait.run_wait_probe(probe["record"], probe["wait"]))

    readers = [threading.Thread(target=read) for _ in range(10)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(timeout=30)
        assert not reader.is_alive()

    assert len(results) == 10
    assert _runs(probe["marker"]) == 1, "the probe ran once per reader"
    assert sum(1 for result in results if result["started"]) == 1
    assert all(result["lines"] == ["pending"] for result in results), (
        "a reader was not handed the result the one run produced"
    )


def test_a_probe_still_running_when_its_interval_comes_round_is_not_started_again(
    tmp_path,
) -> None:
    probe = _probe(tmp_path, 1.0)
    # The interval is compressed here so the arm does not sleep through the
    # real thirty seconds; the constant the gate carries is pinned above.
    first: dict = {}

    def read_first() -> None:
        first.update(
            recovery_wait.run_wait_probe(probe["record"], probe["wait"], interval=0.2)
        )

    worker = threading.Thread(target=read_first)
    worker.start()
    _wait_for_marker(probe["marker"])
    second = recovery_wait.run_wait_probe(probe["record"], probe["wait"], interval=0.2)
    worker.join(timeout=30)
    assert not worker.is_alive()

    assert _runs(probe["marker"]) == 1, (
        "the interval came round while the probe was running and it was started"
    )
    assert second["started"] is False
    assert second["lines"] == ["pending"]


def test_a_probe_past_its_bound_is_stopped_and_raises_one_obligation(
    tmp_path,
) -> None:
    probe = _probe(tmp_path, 90.0)
    started = time.monotonic()
    result = recovery_wait.run_wait_probe(probe["record"], probe["wait"])
    elapsed = time.monotonic() - started

    assert result["stopped"] is True, "the probe was left running past its bound"
    assert elapsed >= recovery_wait.WAIT_PROBE_RUN_BOUND_SECONDS - 1
    pid = int(probe["pid_file"].read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the stopped probe's process is still alive")

    duties = recovery_wait.wait_probe_stopped_duties()
    assert len(duties) == 1, f"expected one obligation, got {duties}"
    duty = duties[0]
    assert duty["run_id"] == probe["record"]["run_id"]
    assert duty["probe"] == " ".join(probe["wait"]["probe"])
    bound = int(recovery_wait.WAIT_PROBE_RUN_BOUND_SECONDS)
    assert f"{bound} s bound" in duty["detail"]

    # The classifier's reader, which a coordinator's row is built from, reports
    # the stopped probe from the same recorded result rather than running it.
    observation = recovery_wait._run_wait_condition_probe(
        probe["record"], probe["wait"]
    )
    assert observation["answered"] is False
    assert observation["detail"] == duty["detail"]


def test_a_failed_probe_runs_again_once_its_interval_has_passed(tmp_path) -> None:
    probe = _probe(tmp_path, 0.0)
    broken = {**probe["wait"], "terminal": ["pending"]}
    first = recovery_wait.run_wait_probe(probe["record"], broken)
    assert first["started"] is True
    finished = time.time()

    cached = recovery_wait.run_wait_probe(probe["record"], broken, now=finished + 1.0)
    assert cached["started"] is False
    assert _runs(probe["marker"]) == 1

    again = recovery_wait.run_wait_probe(probe["record"], broken, now=finished + 31.0)
    assert again["started"] is True
    assert _runs(probe["marker"]) == 2, (
        "a result older than the interval was not refreshed"
    )
