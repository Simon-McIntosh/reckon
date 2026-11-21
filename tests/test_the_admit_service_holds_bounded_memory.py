"""The admit service's memory stays bounded however many passes it has made.

A pass decodes every live pointer and the run documents behind it, then frees
them. The allocator keeps the emptied arenas for the next request, so a
long-lived service's resident set tracked the largest pass it had ever run
rather than the memory it still held: measured at 22.1 GB after three and a
half hours. The service now hands a pass's freed pages back and records the
resident figure on every pass reading, including a pass over a lane with
nothing queued.

Both properties are measured on the service's own process and its own reading,
because both are about what a *later* pass pays for an *earlier* one.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.admission import service


def resident_bytes() -> int:
    """This process's resident set size, read from its own ``/proc`` record."""
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("this kernel does not report VmRSS")


def _write_pointers(count: int, *, generation: int) -> None:
    """Write ``count`` live pointers whose records move with the generation.

    A fleet's pointers are rewritten as its workers report, so the fixture
    rewrites them between passes and its population grows to 2,000 runs. A
    growing fleet is what makes a later pass strictly bigger than an earlier
    one, which is the condition a service that keeps every pass's emptied
    arenas cannot survive. The brief gives each record the size a real one
    carries.
    """
    live = runs.live_dir()
    live.mkdir(parents=True, exist_ok=True)
    moment = datetime.now(tz=UTC).isoformat()
    for index in range(count):
        run_id = f"r-memory-{index:04d}"
        (live / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project": f"project-{index % 4}",
                    "session": f"session-{index % 9}",
                    "phase": "working",
                    "backend": "clive",
                    "created_at": moment,
                    "observed_at": moment,
                    "generation": generation,
                    "launcher_host": socket.gethostname(),
                    "brief": "x" * (4 * 1024),
                    "node": {
                        "id": run_id,
                        "plan": "fixture",
                        "section": "delivery",
                    },
                }
            ),
            encoding="utf-8",
        )


def _run_pass() -> dict[str, object]:
    return service.admit_until_stopped(lane="clive", once=True)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, so no pass reads the fleet's own pointers."""
    (tmp_path / "mounts.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "mounts.json"))
    return tmp_path / "home"


def test_resident_memory_is_flat_across_fifty_passes(crew_home: Path) -> None:
    after_five = 0
    after_fifty = 0
    for index in range(1, 51):
        _write_pointers(200 + index * 36, generation=index)
        _run_pass()
        if index == 5:
            after_five = resident_bytes()
        after_fifty = resident_bytes()
    assert after_five > 0
    assert after_fifty <= after_five * 1.10, (
        f"resident memory grew from {after_five} B after 5 passes to "
        f"{after_fifty} B after 50"
    )


def test_an_idle_pass_writes_a_reading_carrying_its_memory(crew_home: Path) -> None:
    assert not runs.live_dir().exists() or not any(runs.live_dir().glob("*.json"))
    _run_pass()
    log = runs.crew_home() / service._verdicts.ADMISSION_VERDICT_LOG
    readings = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(readings) == 1, readings
    reading = readings[0]
    assert reading["pointers"] == []
    assert reading["resident_bytes"] == service.resident_bytes()
    assert reading["memory_bound_bytes"] == service.ADMIT_MEMORY_BOUND_BYTES