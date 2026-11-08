"""A declared service runs the code the checkout carries, not the code it started on.

Every case drives ``DeclaredServices.reload`` against a stubbed process table
and a stubbed checkout. Nothing here signals a real process: the supervisor's
signal path lands in the table, so a case says what a copy does with SIGTERM and
reads back what was sent. The configuration home, the runtime directory and the
state directory all point at a temporary tree, so no case reads or writes the
machine's own fleet state.

The failure this guards is measured. The admit service and the station kept the
code they started on for days while a fix sat on main: both were parked inside a
pass they could not leave, so a service that reloads itself only between passes
never reached the boundary. Restarting a service whose recorded code differs
from a trusted checkout is what closes that gap.
"""

from __future__ import annotations

import json
import signal
import time
from types import SimpleNamespace

import pytest

from reckon.crew import fleet_supervisor
from reckon.crew.station import StationHeldError

SERVICE = "demo"
# The grace a case hands the supervisor, so the SIGKILL escalation is measured
# without the production ten seconds of real waiting.
STOP_GRACE_SECONDS = 0.2


class FakeCheckout:
    """A checkout's code stamp, and whether the tree carrying it may be loaded."""

    def __init__(
        self, stamp: str = "stamp-new", *, trusted: bool = True, detail: str = ""
    ) -> None:
        self.stamp = stamp
        self.trusted = trusted
        self.detail = detail
        self.trust_reads = 0

    def code_stamp(self) -> str:
        return self.stamp

    def trust(self) -> SimpleNamespace:
        self.trust_reads += 1
        return SimpleNamespace(trusted=self.trusted, detail=self.detail)


class FakeProcesses:
    """A process table the supervisor is driven against, with no real pids.

    A copy is started only while no live copy holds the station's writer lease,
    which is the refusal ``run_station`` raises for a live local holder. That
    makes the order of stop and start observable rather than assumed.
    """

    def __init__(self, checkout: FakeCheckout) -> None:
        self.checkout = checkout
        self.alive: dict[int, str] = {}
        self.ignores_sigterm: set[int] = set()
        self.station_lease: int | None = None
        self.lock_holder: int | None = None
        self.signals: list[tuple[int, int, float]] = []
        self.started: list[int] = []
        self.refusals = 0
        self._next_pid = 90000

    def add_copy(
        self,
        stamp: str,
        *,
        ignores_sigterm: bool = False,
        station_lease: bool = False,
    ) -> int:
        """Register a copy already running on ``stamp``, and return its pid."""
        self._next_pid += 1
        pid = self._next_pid
        self.alive[pid] = stamp
        self.lock_holder = pid
        if ignores_sigterm:
            self.ignores_sigterm.add(pid)
        if station_lease:
            self.station_lease = pid
        return pid

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def lock_held(self) -> bool:
        return self.lock_holder is not None and self.is_alive(self.lock_holder)

    def signal(self, pid: int, signum: int, reason: str = "") -> None:
        self.signals.append((pid, signum, time.monotonic()))
        if signum == signal.SIGTERM and pid in self.ignores_sigterm:
            return
        self._end(pid)

    def _end(self, pid: int) -> None:
        self.alive.pop(pid, None)
        if self.lock_holder == pid:
            self.lock_holder = None

    def start(self) -> int:
        """Start a copy, refusing while a live copy holds the station lease."""
        if self.station_lease is not None and self.is_alive(self.station_lease):
            self.refusals += 1
            raise StationHeldError(
                f"station lease held by live pid {self.station_lease}"
            )
        self._next_pid += 1
        pid = self._next_pid
        self.alive[pid] = self.checkout.stamp
        self.lock_holder = pid
        self.started.append(pid)
        if self.station_lease is not None:
            self.station_lease = pid
        return pid

    def sigterms(self) -> list[int]:
        return [pid for pid, signum, _ in self.signals if signum == signal.SIGTERM]

    def sigkills(self) -> list[int]:
        return [pid for pid, signum, _ in self.signals if signum == signal.SIGKILL]


class FakePopen:
    """The one thing ``_start_locked`` needs from a spawned service."""

    def __init__(self, table: FakeProcesses) -> None:
        self._table = table

    def __call__(self, argv, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(pid=self._table.start())


@pytest.fixture()
def home(tmp_path):
    """A fleet home in a temporary tree, with one declared service."""
    config_home = tmp_path / "config-home"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    for directory in (config_home / "fleet", runtime, state):
        directory.mkdir(parents=True, exist_ok=True)
    (config_home / "fleet" / "services.json").write_text(
        json.dumps({SERVICE: ["python", "-m", "demo"]}), encoding="utf-8"
    )
    return SimpleNamespace(
        tmp_path=tmp_path,
        config_home=config_home,
        runtime=runtime,
        state=state,
        env={
            "FLEET_RUNTIME_DIR": str(runtime),
            "FLEET_STATE_DIR": str(state),
            "XDG_CONFIG_HOME": str(config_home),
        },
    )


def _seed_record(home, pid: int, stamp: str | None) -> None:
    """Record a running copy as an earlier supervisor would have left it."""
    record = f"{pid} -" if stamp is None else f"{pid} - {stamp}"
    fleet_supervisor.service_pid_path(home.runtime, SERVICE).write_text(
        record + "\n", encoding="utf-8"
    )


def _manager(
    home, checkout: FakeCheckout, *, stop_grace: float = STOP_GRACE_SECONDS
) -> fleet_supervisor.DeclaredServices:
    return fleet_supervisor.DeclaredServices(
        home.runtime,
        home.env,
        clock=lambda: 0.0,
        code_stamp=checkout.code_stamp,
        checkout_trust=checkout.trust,
        stop_grace=stop_grace,
    )


def _install(monkeypatch, home, checkout: FakeCheckout) -> FakeProcesses:
    """Point the supervisor's process and signal paths at a fake table."""
    table = FakeProcesses(checkout)
    monkeypatch.setattr(fleet_supervisor, "signal_worker", table.signal)
    monkeypatch.setattr(
        fleet_supervisor,
        "process_runs_service",
        lambda pid, name, argv: table.is_alive(pid),
    )
    monkeypatch.setattr(
        fleet_supervisor, "lock_held", lambda name, runtime: table.lock_held()
    )
    monkeypatch.setattr(
        fleet_supervisor,
        "subprocess",
        SimpleNamespace(Popen=FakePopen(table), DEVNULL=-3),
    )
    return table


def test_a_service_on_other_code_is_restarted_and_the_log_names_both_stamps(
    home, monkeypatch, capsys
) -> None:
    """A copy running other code than a trusted checkout is replaced by one on it."""
    checkout = FakeCheckout("stamp-new", trusted=True)
    table = _install(monkeypatch, home, checkout)
    old_pid = table.add_copy("stamp-old")
    _seed_record(home, old_pid, "stamp-old")

    _manager(home, checkout).reload()

    assert table.sigterms() == [old_pid], "the old copy was not signalled"
    assert not table.is_alive(old_pid), "the old copy survived the restart"
    assert len(table.started) == 1, "a replacement copy was not started"
    new_pid = table.started[0]
    assert fleet_supervisor.recorded_service_pid(home.runtime, SERVICE) == new_pid
    assert fleet_supervisor.recorded_service_stamp(home.runtime, SERVICE) == "stamp-new"
    output = capsys.readouterr().out
    assert "stamp-old" in output and "stamp-new" in output, (
        "the log does not name both stamps, so a reader cannot tell what moved"
    )


def test_a_service_on_the_checkouts_code_is_adopted(home, monkeypatch) -> None:
    """A copy already running the checkout's own code is left exactly as it is."""
    checkout = FakeCheckout("stamp-new", trusted=True)
    table = _install(monkeypatch, home, checkout)
    pid = table.add_copy("stamp-new")
    _seed_record(home, pid, "stamp-new")

    manager = _manager(home, checkout)
    manager.reload()

    assert table.signals == [], "a copy on the checkout's own code was disturbed"
    assert table.started == [], "a second copy was started beside the running one"
    assert manager._states[SERVICE].pid == pid
    assert manager._states[SERVICE].owned is False, "the adopted copy was called owned"
    assert checkout.trust_reads == 0, "the trust probe was spent on a matching stamp"


def test_an_untrusted_checkout_leaves_the_service_running(
    home, monkeypatch, capsys
) -> None:
    """A checkout that may not be loaded is no reason to disturb a running copy."""
    checkout = FakeCheckout(
        "stamp-new", trusted=False, detail="tracked files differ from HEAD"
    )
    table = _install(monkeypatch, home, checkout)
    pid = table.add_copy("stamp-old")
    _seed_record(home, pid, "stamp-old")

    manager = _manager(home, checkout)
    manager.reload()

    assert table.signals == [], "an untrusted checkout stopped a running service"
    assert table.started == [], "an untrusted checkout was started onto"
    assert table.is_alive(pid), "the running copy did not survive"
    assert manager._states[SERVICE].pid == pid
    output = capsys.readouterr().out
    assert "not trusted" in output, "the supervisor did not say why it left the copy"
    assert "tracked files differ from HEAD" in output, (
        "the log does not carry the refusal the trust probe gave"
    )


def test_a_service_that_ignores_sigterm_is_killed_after_the_bound(
    home, monkeypatch
) -> None:
    """A copy that will not stop is killed once the grace has run out."""
    checkout = FakeCheckout("stamp-new", trusted=True)
    table = _install(monkeypatch, home, checkout)
    pid = table.add_copy("stamp-old", ignores_sigterm=True)
    _seed_record(home, pid, "stamp-old")

    _manager(home, checkout).reload()

    assert table.sigterms() == [pid]
    assert table.sigkills() == [pid], "the copy that ignored SIGTERM was never killed"
    term_times = [
        at
        for at_pid, sig, at in table.signals
        if at_pid == pid and sig == signal.SIGTERM
    ]
    kill_times = [
        at
        for at_pid, sig, at in table.signals
        if at_pid == pid and sig == signal.SIGKILL
    ]
    assert kill_times[0] - term_times[0] >= STOP_GRACE_SECONDS, (
        "the copy was killed before its grace had run out"
    )
    assert not table.is_alive(pid), "the killed copy is still recorded as running"
    assert len(table.started) == 1, "no replacement was started after the kill"


def test_a_lease_holding_service_is_started_again_on_the_new_code(
    home, monkeypatch
) -> None:
    """The replacement takes the lease only once the old copy has exited."""
    checkout = FakeCheckout("stamp-new", trusted=True)
    table = _install(monkeypatch, home, checkout)
    # The copy holds the station's writer lease and does not end on SIGTERM, so it
    # is the live holder until the supervisor has killed it: a replacement
    # started in that window is refused exactly as the station refuses one
    # beside a live holder.
    pid = table.add_copy("stamp-old", station_lease=True, ignores_sigterm=True)
    _seed_record(home, pid, "stamp-old")

    _manager(home, checkout).reload()

    assert table.refusals == 0, (
        "the replacement was started while the old copy still held the lease, "
        "so the station refused it"
    )
    assert table.started, "the lease-holding service was never replaced"
    new_pid = table.started[0]
    assert table.alive[new_pid] == "stamp-new", (
        "the replacement is not running the checkout's code"
    )
    assert fleet_supervisor.recorded_service_stamp(home.runtime, SERVICE) == "stamp-new"


def test_a_service_with_no_recorded_stamp_is_restarted_once(home, monkeypatch) -> None:
    """An unrecorded copy is replaced once, and then adopted on its new record."""
    checkout = FakeCheckout("stamp-new", trusted=True)
    table = _install(monkeypatch, home, checkout)
    pid = table.add_copy("stamp-old")
    _seed_record(home, pid, None)
    assert fleet_supervisor.recorded_service_stamp(home.runtime, SERVICE) is None

    _manager(home, checkout).reload()
    assert len(table.started) == 1, "the unrecorded copy was not restarted"
    replacement = table.started[0]
    assert fleet_supervisor.recorded_service_stamp(home.runtime, SERVICE) == "stamp-new"

    # A fresh image, as after a re-exec, reads the new record and adopts it.
    _manager(home, checkout).reload()
    assert len(table.started) == 1, (
        "the copy restarted for its stamp was restarted again on the next check"
    )
    assert table.sigterms() == [pid]
    assert table.is_alive(replacement)
