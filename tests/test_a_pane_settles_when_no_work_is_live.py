"""A session's pane settles when its own work has finished.

A session host runs one follower child per project and session. When the
session's last live run reaches a terminal phase the child leaves with its
settle code; the host stays, and the next request for the pair starts a fresh
child. The judgement of "no live work" is ``_population_has_live_work``'s, not
one re-derived here.

The child prints a line only when the session still owes the coordinator a run:
a drained fleet names nothing to act on, so nothing is printed and the child
leaves with its settle code alone. When it does print, the line names a session
only when this pane carries more than its own: a session at a terminal knows its
own id, so it is named only when the row would otherwise be ambiguous about
which fleet drained.

Two facts decide the whole design, and both are covered below.

A host already running when this landed keeps the code it started with, while a
child it spawns reloads the new code. The host advertises, in the child's
environment, that it understands a settled child; a host that does not set that
variable must keep a follower that never settles, or it would read every settle
as a crash and restart the child on the backoff, repeating the idle line for as
long as the session stayed quiet. So both mixed pairs are exercised: a child
with no advertisement present does not settle, and a new host restarts a child
that exits without the settle status.

A dispatch asks the host for this follower before it publishes the run's
pointer, so a follower that settled on its first empty reading would leave
before its own run appeared. The settle is therefore hinged on the session's
work having been seen live first, and a request that arrives while a child is
still running re-arms the pair rather than leaving it idle.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon import crew_follow_commands as follow_commands
from reckon.crew import needs_you as needs_you_module

REPO_ROOT = Path(__file__).resolve().parents[1]

PROJECT = "settle-proj"
SESSION = "s-settle"
PEER = "s-peer"

POLL_SECONDS = "0.2"
BACKOFF_SECONDS = "0.2"

START_BOUND = 20.0
SETTLE_BOUND = 20.0
STOP_BOUND = 15.0
RESTART_BOUND = 20.0

# The grace handed to a real child below. The production grace bounds an
# admission handover and is far longer than a case here can wait, so the child
# is given a short one through the environment, and every bound above therefore
# measures the follower rather than the grace.
CHILD_SETTLE_GRACE_SECONDS = "0.5"

SETTLE_LINE = "no live work since"


def _module():
    """The session host module as this gate imported it."""
    from importlib import import_module

    return import_module("reckon.crew.session_host")


def _start_time(pid: int) -> str:
    return _module().process_start_time(pid) or ""


def _process_alive(pid: int) -> bool:
    """Whether a pid still runs; a zombie reads as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    state = fields[fields.rfind(")") + 1 :].split()
    return bool(state) and state[0] != "Z"


def _wait_for(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")


def _settle_events(events: list[dict]) -> list[dict]:
    return [
        e for e in events if e.get("event") == follow_commands.FOLLOWER_SETTLE_EVENT
    ]


def _spawn_watch(
    events: list[dict], stop: threading.Event, *, observed: tuple[str, ...] = ()
) -> threading.Thread:
    """Consume the follower's watch generator on a thread, ended by ``stop``."""

    def run() -> None:
        events.extend(
            follow_commands._follow_watch_lines(
                PROJECT,
                session=SESSION,
                observed=observed,
                poll_interval=0.01,
                settle_interval=0.01,
                sweep=None,
                settle=True,
                stop=stop,
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# ── the follower's own settle judgement, in process ─────────────────────────


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project config home under the follower's own ``RECKON_HOME``."""
    config_home = tmp_path / "home"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_unpromoted(run_id: str, *, session: str, home: Path) -> None:
    """End one run without promoting it: drained work the coordinator still owes.

    A terminal phase drains the pane, and a manifest reading ``complete`` with
    the pointer still live leaves the run unpromoted, so the settle row's tail
    names it. That is the state in which a printed settle line is observable.
    """
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\nblockers: none\n", encoding="utf-8"
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": "complete",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "manifest_path": str(manifest),
            "process_alive": None,
        },
    )


def _write_pointer(run_id: str, *, session: str, phase: str) -> None:
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "process_alive": None,
        },
    )


def test_a_child_settles_once_its_session_work_drains(home: Path) -> None:
    """The idle line is delivered once and the child ends (case 1)."""
    _write_pointer("r-mine", session=SESSION, phase="working")
    events: list[dict] = []
    stop = threading.Event()
    thread = _spawn_watch(events, stop)
    try:
        # Let the follower see the work live before the run drains.
        time.sleep(0.5)
        _write_pointer("r-mine", session=SESSION, phase="complete")
        thread.join(SETTLE_BOUND)
        assert not thread.is_alive(), "the child did not leave after it settled"
        settles = _settle_events(events)
        assert len(settles) == 1, settles
        # The fleet drained with nothing outstanding: the row prints no line,
        # because it names nothing the coordinator can act on. The row is still
        # emitted, so the child's last row remains the settle its host retires.
        assert settles[0]["line"] == "", settles[0]["line"]
        assert events[-1] is settles[0], "the settle row is the child's last row"
    finally:
        stop.set()
        thread.join(5.0)


def test_a_child_that_never_saw_work_live_does_not_settle(home: Path) -> None:
    """A child armed into an idle session keeps following.

    A dispatch arms the follower just before it publishes the run's pointer, so
    an empty population at that instant is the normal state rather than the end
    of the work. The same reader that settles a session whose work drained stays
    put for one that was never live -- the positive control for the case above.
    """
    events: list[dict] = []
    stop = threading.Event()
    thread = _spawn_watch(events, stop)
    try:
        time.sleep(0.5)
        assert not _settle_events(events), "the child settled before any work was live"
    finally:
        stop.set()
        thread.join(5.0)


def test_an_observed_peers_live_run_keeps_the_child_following(home: Path) -> None:
    """An observed peer's live run keeps the pane open (case 2a).

    The pane carries its own session's work and every session it was asked to
    observe, so an observed peer's run is work still owed the pane.
    """
    _write_pointer("r-mine", session=SESSION, phase="working")
    _write_pointer("r-peer", session=PEER, phase="working")
    events: list[dict] = []
    stop = threading.Event()
    thread = _spawn_watch(events, stop, observed=(PEER,))
    try:
        time.sleep(0.5)
        _write_pointer("r-mine", session=SESSION, phase="complete")
        time.sleep(0.5)
        assert not _settle_events(events), "the child settled under an observed peer"
        # The positive control: with the observed peer drained too, the same
        # reader settles.
        _write_pointer("r-peer", session=PEER, phase="complete")
        time.sleep(0.5)
        assert _settle_events(events), "the child never settled once every run drained"
    finally:
        stop.set()
        thread.join(5.0)


def test_a_settle_prints_no_line_even_with_an_outstanding_run(home: Path) -> None:
    """A settle emits its event and no line, whatever the pane observes.

    The row is how the child's host retires it, and it names nothing: an
    outstanding run is the obligations list's to name, not a sentence composed
    here. The pane's own run ends unpromoted, so the derivation behind that
    list still holds it while the pane goes quiet.
    """
    _write_pointer("r-mine", session=SESSION, phase="working")
    _write_pointer("r-peer", session=PEER, phase="working")
    events: list[dict] = []
    stop = threading.Event()
    thread = _spawn_watch(events, stop, observed=(PEER,))
    try:
        time.sleep(0.5)
        _write_pointer("r-peer", session=PEER, phase="complete")
        _write_unpromoted("r-mine", session=SESSION, home=home)
        time.sleep(1.0)
        settles = _settle_events(events)
        assert len(settles) == 1, settles
        assert settles[0]["line"] == "", settles[0]["line"]
        assert "r-mine" not in json.dumps(settles[0]), settles[0]
        owed = needs_you_module.needs_you_runs(PROJECT, session=SESSION)
        assert [row["run_id"] for row in owed] == ["r-mine"], owed
    finally:
        stop.set()
        thread.join(5.0)


def test_an_unobserved_peers_live_run_does_not_hold_the_child_open(home: Path) -> None:
    """An unobserved peer's live run is not this pane's work (case 2b).

    The counterpart of the case above, and the reason the population is not the
    whole project: a peer the follower was never asked to observe produces no
    row this pane prints, so its run must not keep the pane alive.
    """
    _write_pointer("r-mine", session=SESSION, phase="working")
    _write_pointer("r-peer", session=PEER, phase="working")
    events: list[dict] = []
    stop = threading.Event()
    thread = _spawn_watch(events, stop)
    try:
        time.sleep(0.5)
        _write_pointer("r-mine", session=SESSION, phase="complete")
        time.sleep(1.0)
        assert _settle_events(events), (
            "the child stayed open under an unobserved peer's live run"
        )
    finally:
        stop.set()
        thread.join(5.0)


# ── the host and its child over a temporary host state ──────────────────────


class _OwnerStub:
    """A stand-in for the Claude process a session host lives for."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.pid = self.process.pid
        deadline = time.monotonic() + START_BOUND
        self.start_time = _start_time(self.pid)
        while not self.start_time and time.monotonic() < deadline:
            time.sleep(0.01)
            self.start_time = _start_time(self.pid)
        assert self.start_time, "owner stub has no readable start time"

    def end(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


# A child that understands the settle protocol: when the host advertises
# support it prints its idle line and leaves with the settle status; otherwise
# it keeps running -- exactly the child an old host must never see exit.
SETTLING_FAKE = """\
import os, sys, time
marker = os.environ.get('FAKE_MARKER')
if marker:
    with open(marker, 'a', encoding='utf-8') as handle:
        handle.write(f'{os.getpid()}\\n')
print('FAKE-FOLLOWER-LINE', flush=True)
if os.environ.get('RECKON_SESSION_HOST_SETTLE') == '1':
    sys.exit(66)
while True:
    time.sleep(0.05)
"""

# An old-style child: it never settles, whatever the host advertises.
OLD_STYLE_FAKE = """\
import os, sys, time
marker = os.environ.get('FAKE_MARKER')
if marker:
    with open(marker, 'a', encoding='utf-8') as handle:
        handle.write(f'{os.getpid()}\\n')
print('FAKE-FOLLOWER-LINE', flush=True)
while True:
    time.sleep(0.05)
"""

# A child that ignores the protocol and exits at once: a failure, not a settle.
EXIT_ZERO_FAKE = """\
import os
marker = os.environ.get('FAKE_MARKER')
if marker:
    with open(marker, 'a', encoding='utf-8') as handle:
        handle.write(f'{os.getpid()}\\n')
"""


class _Host:
    """A running session host and everything a case reads about it."""

    def __init__(self, tmp_path: Path, follower_source: str) -> None:
        self.tmp_path = tmp_path
        self.marker = tmp_path / "children.txt"
        self.fake_follower = tmp_path / "fake_follower.py"
        self.fake_follower.write_text(follower_source, encoding="utf-8")
        self.state_dir = tmp_path / "state"
        self.log_dir = tmp_path / "logs"
        self.home = tmp_path / "home"
        self.stdout = tmp_path / "host.stdout"
        self.stderr = tmp_path / "host.stderr"
        self.owner = _OwnerStub()
        self._handles: list = []
        self._process: subprocess.Popen | None = None

    def env(self) -> dict[str, str]:
        environ = dict(os.environ)
        environ["PYTHONPATH"] = str(REPO_ROOT)
        environ["RECKON_HOME"] = str(self.home)
        environ["RECKON_SESSION_HOST_STATE_DIR"] = str(self.state_dir)
        environ["RECKON_SESSION_HOST_LOG_DIR"] = str(self.log_dir)
        environ["RECKON_SESSION_HOST_POLL_SECONDS"] = POLL_SECONDS
        environ["RECKON_SESSION_HOST_BACKOFF"] = BACKOFF_SECONDS
        environ["FAKE_MARKER"] = str(self.marker)
        environ["XDG_RUNTIME_DIR"] = str(self.tmp_path / "run")
        environ.pop("CLAUDE_PID", None)
        return environ

    def start(self) -> None:
        out = self.stdout.open("wb")
        err = self.stderr.open("wb")
        self._handles = [out, err]
        argv = [
            _module()._reckon_console_script(),
            "crew",
            "host",
            "--owner-pid",
            str(self.owner.pid),
            "--owner-start",
            self.owner.start_time,
            "--follower-command",
            json.dumps([sys.executable, str(self.fake_follower)]),
        ]
        self._process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=out, stderr=err, env=self.env()
        )

    def request(self, project: str, session: str) -> None:
        assert self._process is not None and self._process.stdin is not None
        line = json.dumps({"project": project, "session": session}) + "\n"
        self._process.stdin.write(line.encode())
        self._process.stdin.flush()

    def child_pids(self) -> list[int]:
        if not self.marker.exists():
            return []
        return [
            int(line)
            for line in self.marker.read_text(encoding="utf-8").split()
            if line.strip()
        ]

    def stdout_text(self) -> str:
        return self.stdout.read_text(encoding="utf-8") if self.stdout.exists() else ""

    def record_path(self) -> Path:
        return self.state_dir / f"{self.owner.pid}-{self.owner.start_time}.json"

    def record(self) -> dict:
        try:
            return json.loads(self.record_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=STOP_BOUND)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        for handle in self._handles:
            handle.close()
        self.owner.end()


@pytest.fixture()
def settling_host(tmp_path: Path):
    harness = _Host(tmp_path, SETTLING_FAKE)
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture()
def old_style_host(tmp_path: Path):
    harness = _Host(tmp_path, OLD_STYLE_FAKE)
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()


def _retired(harness: _Host) -> bool:
    """Whether the census lists no running child: the pair has been retired."""
    record = harness.record()
    return "children" in record and not record["children"]


def test_the_host_retires_a_settled_child_and_stays_alive(settling_host: _Host) -> None:
    """The idle line prints once and the host survives it (case 1, host half).

    This is the case the negative control must fail: with the settle status
    unrecognised, the host restarts the child on the backoff and the line
    repeats.
    """
    settling_host.request(PROJECT, SESSION)
    _wait_for(
        lambda: len(settling_host.child_pids()) >= 1,
        timeout=START_BOUND,
        description="a child",
    )
    _wait_for(
        lambda: _retired(settling_host),
        timeout=SETTLE_BOUND,
        description="the pair retired from the census",
    )
    time.sleep(2.0)
    assert len(settling_host.child_pids()) == 1, settling_host.child_pids()
    # One child, so one arrival line: a host that restarted the settled child
    # would show a second.
    assert settling_host.stdout_text().count("FAKE-FOLLOWER-LINE") == 1
    assert settling_host._process is not None
    assert _process_alive(settling_host._process.pid), "the host exited on the settle"


def test_a_later_request_starts_one_new_child(settling_host: _Host) -> None:
    """The next request re-arms the settled pair (case 3, host half)."""
    settling_host.request(PROJECT, SESSION)
    _wait_for(
        lambda: _retired(settling_host),
        timeout=SETTLE_BOUND,
        description="the first pair retired",
    )
    settling_host.request(PROJECT, SESSION)
    _wait_for(
        lambda: len(settling_host.child_pids()) == 2,
        timeout=START_BOUND,
        description="one new child after the settle",
    )
    time.sleep(0.5)
    assert len(set(settling_host.child_pids())) == 2, settling_host.child_pids()


def test_two_requests_together_start_one_child(old_style_host: _Host) -> None:
    """Two requests arriving together start one child (case 4)."""
    old_style_host.request(PROJECT, SESSION)
    _wait_for(
        lambda: len(old_style_host.child_pids()) == 1,
        timeout=START_BOUND,
        description="a child",
    )
    old_style_host.request(PROJECT, SESSION)
    old_style_host.request(PROJECT, SESSION)
    time.sleep(1.0)
    assert len(old_style_host.child_pids()) == 1, old_style_host.child_pids()


def test_a_new_host_keeps_an_old_style_child_alive(old_style_host: _Host) -> None:
    """A new host runs a child that never settles without retiring it.

    The mixed pair on the other side: the host understands a settled child, but
    this child is not one: it stays running, and the host leaves it running.
    """
    old_style_host.request(PROJECT, SESSION)
    _wait_for(
        lambda: len(old_style_host.child_pids()) == 1,
        timeout=START_BOUND,
        description="a child",
    )
    time.sleep(1.5)
    assert len(old_style_host.child_pids()) == 1, old_style_host.child_pids()
    child = old_style_host.child_pids()[0]
    assert _process_alive(child), "the host retired a child that never settled"


def test_a_new_host_restarts_a_child_that_exits_without_settling(
    tmp_path: Path,
) -> None:
    """An exit without the settle status is a restart, not a retirement.

    The child here ignores the protocol and exits at once; the host must treat
    that as the ordinary exit it always did: restart on the backoff. The
    positive control for the retirement above.
    """
    harness = _Host(tmp_path, EXIT_ZERO_FAKE)
    harness.start()
    try:
        harness.request(PROJECT, SESSION)
        _wait_for(
            lambda: len(harness.child_pids()) >= 2,
            timeout=RESTART_BOUND,
            description="a restarted child",
        )
    finally:
        harness.stop()


# ── the real follower as a child, through the host's advertisement ──────────


def _child_env(home: Path, tmp_path: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("RECKON_", "CLAUDE_", "CODEX_"))
    }
    env["RECKON_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["XDG_RUNTIME_DIR"] = str(tmp_path / "run")
    # Named here rather than inherited: the filter above drops every RECKON_
    # variable, and a child left on the production grace would hold a pane far
    # past every bound this file waits on.
    env[follow_commands.FOLLOWER_SETTLE_GRACE_ENV] = CHILD_SETTLE_GRACE_SECONDS
    return env


def _follower_registered(home: Path, session: str) -> bool:
    """Whether the child has published its follower lease under ``home``.

    The lease is written by the child's registration, before it reads the fleet,
    so its presence proves the pane is up and about to sample the population --
    unlike a fixed sleep, which races the child's startup against the drain on a
    loaded node.
    """
    watch = home / "crew" / "watch"
    return any(watch.glob(f"*.followers/{session}-*.lease"))


def _start_real_follower(env: dict[str, str]) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        "follow",
        "--project",
        PROJECT,
        "--session",
        SESSION,
        "--no-color",
    ]
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )


def _end(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=STOP_BOUND)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_the_real_follower_settles_when_the_host_advertises_it(
    home: Path, tmp_path: Path
) -> None:
    """A real follower, advertised by its host, prints nothing and leaves.

    The whole path end to end: the advertisement is set, the follower sees the
    session's work drain, prints no line because nothing is outstanding, and
    still exits with the settle status its host keys on.
    """
    _write_pointer("r-mine", session=SESSION, phase="working")
    env = _child_env(home, tmp_path)
    env[_module().SETTLE_ENV] = "1"
    process = _start_real_follower(env)
    try:
        # The follower needs its imports before its first settle sample, and a
        # drain that lands before that sample is a population the pane never saw
        # hold work, which it must not settle on. So the work stays live until
        # the child is provably up and reading -- its own registration lease,
        # not a fixed sleep that races its startup on a loaded node.
        _wait_for(
            lambda: _follower_registered(home, SESSION),
            timeout=START_BOUND,
            description="the child to register its follower lease",
        )
        _write_pointer("r-mine", session=SESSION, phase="complete")
        _wait_for(
            lambda: process.poll() is not None,
            timeout=SETTLE_BOUND,
            description="the follower to settle and leave",
        )
        assert process.poll() == _module().SETTLED_EXIT_CODE, process.poll()
        # The fleet drained with nothing outstanding, so no idle line is
        # printed; the child still leaves with the settle status its host keys
        # on, which is what retires the pair.
        text = process.stdout.read() if process.stdout else ""
        assert SETTLE_LINE not in text, text
    finally:
        _end(process)


def test_a_real_follower_with_no_advertisement_never_settles(
    home: Path, tmp_path: Path
) -> None:
    """An old host starts a new child that keeps following (mixed pair 1).

    A host already running when this landed does not set the advertisement, so
    the follower it starts cannot settle; it keeps following rather than
    leaving, and the old host never has to read its exit as a settle.
    """
    _write_pointer("r-mine", session=SESSION, phase="working")
    env = _child_env(home, tmp_path)
    assert _module().SETTLE_ENV not in env
    process = _start_real_follower(env)
    try:
        # The follower needs its imports before its first settle sample, so the
        # work stays live until the child is provably up and reading.
        time.sleep(3.0)
        _write_pointer("r-mine", session=SESSION, phase="complete")
        time.sleep(2.5)
        assert process.poll() is None, "the child left under an old host"
    finally:
        _end(process)
