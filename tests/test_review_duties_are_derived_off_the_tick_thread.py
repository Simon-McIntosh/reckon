"""Review duties are derived on a worker, so a long derivation holds no tick.

The station publishes each project's run-phase transitions on its tick, and a
transition is what wakes the session that owns the run. Deriving that project's
review duties reads the review store once per run and can outlast a tick many
times over, so a derivation run on the tick path holds every later tick's
transitions behind it: a run that finishes during a long derivation is not
published until the derivation returns, and its session is told nothing for as
long as that takes.

The station therefore derives each project's duties on a worker it owns. The
tick still asks for the derivation and still records the stage it asked in, so
a reader that sees a tick standing in that stage is reading a derivation
standing between two ticks, not a station that stopped.

The case drives ``run_station`` itself, the entry point a real station takes,
across several tick intervals. The first tick's derivation is held open, and
a run's phase then changes between two ticks. The transition that change
belongs to must be published while the derivation is still blocked, and the
project must never have a second derivation running beside the first.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import station, station_obligations, station_store

PROJECT = "duty-derived-fixture"
SESSION = "session-one"
RUN_ID = "r-duty-derived"


def _write_pointer(home: Path, *, phase: str) -> Path:
    """One live pointer, shaped as the launcher and the supervisor write it."""
    stream = home / "runs" / RUN_ID / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    if not stream.exists():
        stream.write_text(
            json.dumps({"type": "system", "subtype": "init"}) + "\n", encoding="utf-8"
        )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "created_at": "2026-01-01T00:10:00Z",
        "launcher_host": socket.gethostname(),
        "node": {"id": RUN_ID},
        "reason": f"now {phase}",
        "log_path": str(stream),
        "worktree": str(home / "trees" / RUN_ID),
    }
    path = home / "live" / f"{RUN_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _append_stream_line(pointer: Path) -> None:
    """Move the stream reader, as a delivering worker's next line would."""
    stream = Path(json.loads(pointer.read_text(encoding="utf-8"))["log_path"])
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
        )


class _Sweeps:
    """The station's sweep arm, run where the case can see it."""

    def __call__(self) -> dict:
        return {}


def _sweep_beat() -> station.SweepBeat:
    return station.SweepBeat(sweep=_Sweeps(), start=lambda action: action())


@pytest.mark.timeout(90)
def test_a_blocked_derivation_does_not_hold_the_next_ticks_transition(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "station"
    pointer = _write_pointer(home, phase="working")
    node = station.Station(
        station_store.StationStore(root),
        home=home,
        projects=dict,
        sweeps=_sweep_beat(),
    )

    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    derived: list[str] = []

    original_publish = station_obligations.publish

    def held_publish(store, *, project, home, changed, now=None, submit=None):
        # The tick's own call names the worker to hand the derivation to; the
        # worker's call comes back here with no such name, and is held open.
        if submit is not None:
            return original_publish(
                store,
                project=project,
                home=home,
                changed=changed,
                now=now,
                submit=submit,
            )
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            derived.append(project)
        try:
            entered.set()
            assert release.wait(30), "the derivation was never released"
        finally:
            with lock:
                active -= 1
        return None

    monkeypatch.setattr(station_obligations, "publish", held_publish)

    started: list[float] = []
    real_tick = node.tick

    def counted_tick(*, now=None):
        if release.is_set() and len(started) >= 2:
            raise station_store.StationLeaseLostError("the case has seen its ticks")
        started.append(time.monotonic())
        return real_tick(now=datetime.now(tz=UTC))

    monkeypatch.setattr(node, "tick", counted_tick)

    failures: list[BaseException] = []

    def drive() -> None:
        try:
            station.run_station(
                store=node.store,
                station_instance=node,
                interval=0.05,
            )
        except station.StationHeldError:
            pass
        except BaseException as error:  # noqa: BLE001 - carried to the case
            failures.append(error)

    worker = threading.Thread(target=drive, name="station", daemon=True)
    worker.start()
    reader = station_store.StationStore(root)
    try:
        assert entered.wait(30), (
            "the first tick never derived its duties; "
            f"ticks={len(started)} failures={failures!r}"
        )
        # The run moves while the derivation is still blocked. Only a later
        # tick can publish that transition.
        _write_pointer(home, phase="blocked-upstream")
        _append_stream_line(pointer)
        deadline = time.monotonic() + 20
        moved: dict[str, tuple] = {}
        while time.monotonic() < deadline:
            moved = {
                str(row.get("run")): (row.get("from_state"), row.get("to_state"))
                for row in reader.events()
                if row.get("kind") == "transition"
            }
            if moved.get(RUN_ID) == ("working", "blocked-upstream"):
                break
            time.sleep(0.02)
        assert moved.get(RUN_ID) == ("working", "blocked-upstream"), (
            "the transition a later tick publishes waited for the blocked "
            f"derivation: published={sorted(moved)} ticks={len(started)}"
        )
        assert not release.is_set(), "the derivation was not held across both ticks"
        assert len(started) >= 2, (
            f"only {len(started)} tick(s) ran while the derivation was blocked"
        )
        with lock:
            assert peak == 1, f"{peak} derivations ran at once"
            assert derived, "no derivation ran on the station's worker"
        assert node.duties.in_flight() == {PROJECT}, node.duties.in_flight()
    finally:
        release.set()
        worker.join(60)
        reader.close()

    assert not failures, failures
    assert not worker.is_alive(), "the station thread outlived its lease loss"
