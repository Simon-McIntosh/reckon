"""A service that replaces its image waits for the dispatches it owes a worker.

A dispatch cuts a node's worktree and composes a prompt after it takes the node
claim and before it starts a worker. Every service that reloads itself calls
``os.execve``, which ends all its threads at once, so a dispatch caught in that
span loses its thread and leaves a claim behind with no worker to justify it.
The services therefore reach one wait, and this file proves the wait is on the
path each of them actually takes to ``os.execve``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Self

import pytest

from reckon import checkout_trust, cli
from reckon.crew import (
    dispatch_claims as claims,
)
from reckon.crew import (
    follower_reload,
    queue_admission,
    station,
)
from reckon.crew import (
    runs as runs_module,
)

#: How long a held dispatch stays outstanding before it reaches its worker.
#: Long enough that an unwaiting exec is certain to run inside the window, and
#: short enough that three cases cost under a second.
HOLD_SECONDS = 0.3

#: The bound the cases run under. Large, so a service that waits is never
#: abandoned by it; the bound case patches its own small value.
BOUND_SECONDS = 30.0


@pytest.fixture(autouse=True)
def reexec_bound(monkeypatch: pytest.MonkeyPatch) -> object:
    """Hold the register empty between cases and set the wait's bound.

    The register is process-wide, so a case that fails mid-flight would seed
    the next one with an outstanding dispatch it never asked for.
    """
    monkeypatch.setattr(claims, "REEXEC_DISPATCH_WAIT_SECONDS", BOUND_SECONDS)
    claims._DISPATCHES_IN_FLIGHT.clear()
    yield
    claims._DISPATCHES_IN_FLIGHT.clear()


class _DispatchInFlight:
    """One dispatch between its claim and its worker, held open by a thread.

    The thread stands in for the sweep, the producer or an admission pass: it
    owns the dispatch and reaches its worker a moment after the service does,
    which is exactly the overlap the wait exists for.
    """

    def __init__(self, run_id: str = "r-in-flight", node_id: str = "node-waiting"):
        self.run_id = run_id
        self.node_id = node_id
        self.released = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        time.sleep(HOLD_SECONDS)
        claims._leave_dispatch_in_flight(self.run_id)
        self.released.set()

    def __enter__(self) -> Self:
        claims._enter_dispatch_in_flight(self.run_id, self.node_id)
        # A positive control on the instrument: a register that was never
        # seeded would make every case below pass without a wait at all.
        assert claims.dispatches_in_flight() == {self.run_id: self.node_id}
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.released.wait()


def _recording_execve(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Stub ``os.execve``, recording which dispatches were outstanding at it.

    The replacement never happens: the stub records, then raises the ``OSError``
    a failed exec would raise, so each service takes the path it already has for
    one. What the record holds is the whole assertion — an empty mapping at exec
    means the service waited for everything it had in flight.
    """
    at_exec: list[dict[str, str]] = []

    def execve(_path: str, _argv: list[str], _env: dict[str, str]) -> None:
        at_exec.append(claims.dispatches_in_flight())
        raise OSError("execve captured, not executed")

    monkeypatch.setattr(os, "execve", execve)
    return at_exec


def test_the_station_waits_for_a_dispatch_before_it_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The station's sweep thread is the dispatcher that most often overlaps."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    reloader = station.StationReloader(runtime_root)
    reloader.identity = "0" * 40
    monkeypatch.setattr(
        checkout_trust, "source_code_identity", lambda *a, **k: "1" * 40
    )
    image = runtime_root / "reload-trees" / "key" / ("1" * 40)
    image.mkdir(parents=True)
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
    # A truthy import list is the whole of what the reload reads here: an empty
    # one defers the reload instead of exec-ing, and no dispatch would be at
    # risk.
    monkeypatch.setattr(checkout_trust, "reckon_imports", lambda root: (["reckon"], ""))
    monkeypatch.setattr(follower_reload, "image_reexec_environment", lambda *a, **k: {})
    at_exec = _recording_execve(monkeypatch)

    with _DispatchInFlight(), pytest.raises(OSError, match="execve captured"):
        reloader.poll()

    assert at_exec == [{}], f"the station exec'd with {at_exec} in flight"


def test_the_follower_waits_for_a_dispatch_before_it_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follower's reload happens on the same thread that owns its readers."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    stamps = iter(["old-stamp", "new-stamp"])
    monkeypatch.setattr(runs_module, "follower_code_stamp", lambda: next(stamps))
    # The case runs inside a tree a node is editing, which the reloader would
    # refuse to leave; the clean-tree verdict is the one a checkout at rest
    # carries, and the reload it gates is what this case is about.
    monkeypatch.setattr(
        checkout_trust, "tracked_reckon_tree_is_dirty", lambda *a, **k: (False, "")
    )
    reloader = cli._FollowerReloader("proj", None)
    # The replacement's import proof runs a throwaway interpreter, which takes
    # longer than the held dispatch is held. Answered as a proved image, the
    # case turns on the wait and not on how fast this host imports.
    monkeypatch.setattr(reloader, "_replacement_imports", lambda: (True, ""))
    at_exec = _recording_execve(monkeypatch)

    with _DispatchInFlight():
        reloader.poll({})

    assert at_exec == [{}], f"the follower exec'd with {at_exec} in flight"


def test_the_admit_service_waits_for_a_dispatch_before_it_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admission pass launches workers, so the service owes them a wait."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(
        queue_admission, "_source_root", lambda: checkout, raising=False
    )
    at_exec = _recording_execve(monkeypatch)

    with _DispatchInFlight(), pytest.raises(OSError, match="execve captured"):
        queue_admission._reexec_into_current_code()

    assert at_exec == [{}], f"the admit service exec'd with {at_exec} in flight"


def test_a_dispatch_still_in_flight_at_the_bound_is_logged_by_node(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wait is bounded, and what it gives up on it names by node.

    A dispatch that never reaches its worker must not hold a reload open for
    ever, and a claim reclaimed afterwards has to be traceable to the service
    that left it — so the abandoned dispatch is named, by node, where the
    service's own log reads it.
    """
    monkeypatch.setattr(claims, "REEXEC_DISPATCH_WAIT_SECONDS", 0.05)
    claims._enter_dispatch_in_flight("r-stuck", "node-stuck")

    abandoned = claims.wait_for_dispatches_in_flight()

    assert abandoned == (("r-stuck", "node-stuck"),)
    assert "node-stuck" in capsys.readouterr().err


def test_a_wait_with_nothing_in_flight_logs_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ordinary steady state is a clean reload with no line at all."""
    assert claims.wait_for_dispatches_in_flight() == ()
    assert capsys.readouterr().err == ""
