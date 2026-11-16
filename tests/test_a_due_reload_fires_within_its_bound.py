"""A due reload fires within its bound however slow the ticks around it are.

``StationReloader`` holds a due reload while the derivations in flight land, for
at most ``RELOAD_HOLD_SECONDS``. The hold used to be dated from the tick that
noticed the code move, and the reloader was only polled at a tick's end, so a
tick several minutes long pushed the deadline out by its own length: on 15
November a station served code 100 minutes older than the branch while the fix
that would have made it fast sat unexecuted.

A deferred image had the same shape of defect from the other side: it was never
proved again until main moved. A deferral that says nothing about the image --
a proof that timed out, or a peer holding the image's proof lock -- therefore
held the station on stale code for exactly as long as the branch stood still.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import checkout_trust
from reckon.crew import follower_reload, station

OLD = "0" * 40
NEW = "1" * 40

#: A tick longer than the hold bound, so a poll at its end is the station's
#: only chance to notice the code moved or the bound ran out.
SLOW_TICK_SECONDS = 30.0


class Duties:
    """The duty beat's two answers, without a derivation behind them."""

    def __init__(self) -> None:
        self.in_flight = False
        self.quiesced = 0
        self.resumed = 0

    def quiesce(self) -> bool:
        self.quiesced += 1
        return not self.in_flight

    def resume(self) -> None:
        self.resumed += 1


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The monotonic clock this station reads, advanced by the case."""
    now = [0.0]
    monkeypatch.setattr(station, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def reloader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A reloader whose committed code has moved, with the exec recorded."""
    runtime_root = tmp_path / "runtime"
    image = runtime_root / "reload-trees" / "key" / NEW
    image.mkdir(parents=True)
    identity = [OLD]
    proven = [True]
    probes: list[str] = []
    monkeypatch.setattr(
        checkout_trust, "source_code_identity", lambda *a, **k: identity[0]
    )
    monkeypatch.setattr(
        checkout_trust,
        "materialize_committed_tree",
        lambda root, revision, *, active_root=None: (
            image,
            [],
            [],
            os.open(os.devnull, os.O_RDONLY),
        ),
    )

    def reckon_imports(root: Path) -> tuple[bool, str]:
        probes.append(str(root))
        if proven[0]:
            return True, ""
        return False, "pydantic is missing"

    monkeypatch.setattr(checkout_trust, "reckon_imports", reckon_imports)
    monkeypatch.setattr(follower_reload, "image_reexec_environment", lambda *a, **k: {})
    execs: list[str] = []

    def execve(_path: str, _argv: list[str], _env: dict[str, str]) -> None:
        execs.append("exec")
        raise OSError("execve captured, not executed")

    monkeypatch.setattr(os, "execve", execve)
    active = station.StationReloader(runtime_root)
    active.identity = OLD
    return SimpleNamespace(
        reloader=active, execs=execs, identity=identity, probes=probes, proven=proven
    )


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> float:
    """A hold bound shorter than a tick, so one slow tick spans its expiry."""
    monkeypatch.setattr(station, "RELOAD_HOLD_SECONDS", 20.0)
    return 20.0


def test_a_reload_due_through_a_slow_tick_fires_within_one_tick_of_its_bound(
    clock, reloader, bound
):
    """The bound runs from when the code moved, not from the tick that saw it.

    The station polls at a tick's end and nowhere else, so a tick longer than
    the station's hold bound leaves no poll between the code moving and the
    bound expiring. Dating the hold from that poll put the deadline a whole
    tick past the bound, and the reload only fired on the tick after the tick
    that had already outlasted it.
    """
    duties = Duties()
    reloader.reloader.poll(duties)
    assert reloader.execs == []
    # The branch moves to NEW while this tick runs, and the tick outlasts the
    # bound. A derivation is still landing when it ends.
    reloader.identity[0] = NEW
    duties.in_flight = True
    clock[0] = SLOW_TICK_SECONDS
    with pytest.raises(OSError, match="execve captured"):
        reloader.reloader.poll(duties)
    assert reloader.execs == ["exec"]


def test_a_deferred_image_is_proved_again_at_the_next_poll(clock, reloader):
    """A refusal that was not about the image is not waited on until main moves.

    The proof a station runs on a published image is remembered, but a timeout
    and a peer's held lock are not: both refuse without saying anything about
    the image, and either can pass a moment later. The deferred image is
    therefore proved again at the next poll rather than at the next landing.
    """
    reloader.identity[0] = NEW
    reloader.proven[0] = False
    reloader.reloader.poll()
    assert reloader.execs == []
    reloader.proven[0] = True
    with pytest.raises(OSError, match="execve captured"):
        reloader.reloader.poll()
    assert reloader.execs == ["exec"]


def test_a_repeatedly_deferred_image_waits_its_spacing(clock, reloader):
    """An image that will not import is not proved on every tick.

    The retry at the next poll answers the reload; repeating it at the station's
    tick rate would run an import probe per tick for an image that has not
    changed, so a deferral that repeats waits a spacing before its next proof.
    """
    reloader.identity[0] = NEW
    reloader.proven[0] = False
    reloader.reloader.poll()
    reloader.reloader.poll()
    assert len(reloader.probes) == 2
    reloader.reloader.poll()
    assert len(reloader.probes) == 2
    # The second deferral spaced its own next proof.
    clock[0] = station.RELOAD_DEFERRED_RETRY_SECONDS
    reloader.reloader.poll()
    assert len(reloader.probes) == 3


def test_a_reload_due_with_nothing_in_flight_fires_at_the_first_poll(clock, reloader):
    """No derivation to wait for means no hold, whatever the tick length."""
    duties = Duties()
    reloader.identity[0] = NEW
    clock[0] = SLOW_TICK_SECONDS
    with pytest.raises(OSError, match="execve captured"):
        reloader.reloader.poll(duties)
    assert reloader.execs == ["exec"]
    assert duties.quiesced == 1
