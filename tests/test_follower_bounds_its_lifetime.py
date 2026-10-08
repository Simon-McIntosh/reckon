"""The follower's lifetime bounds a poll, a reload, its children, and the refusal that teaches it.

Four properties, each with its own measure, all about the same arming.

A poll the follower runs is bounded by the arming's deadline, not merely the
gap between two of them: the recovery sweep walks every registered worktree and
can run for hours, and a follower armed for 29 m sat inside one for 3 h 36 m
while the check that ends an arming waited for it to return. The first case
stubs the poll to block past the deadline and requires the follower to end
within one second of it, with the poll still blocked — abandoned, not returned.

A reloaded follower ends within the original grant plus one poll interval. The
deadline is an absolute instant fixed at the first arming, and the replacement
image must spend what is left of it rather than re-anchoring at its own start,
which charged each reload for that image's setup — measured at up to 2.6 s past
a six-second grant. The case enters as a replacement does, with the carried
instant in the environment and the same command line, and its clock is injected
with the setup priced on that clock a known interval, so the difference is
exact rather than a race: an image that re-anchors ends that interval past the
case's slack, every run.

No child of an armed follower carries ``RECKON_FOLLOWER_CHECKPOINT`` or
``RECKON_WATCH_ARMING``. Both are hand-offs to one process, and both were placed
in ``os.environ``, which every child inherits — the defect the lifetime deadline
had before it was carried to the replacement alone. A parked run's declared
probe records the environment it is handed, so the property is observed in the
one place a leaked variable would show, and the instrument is proved to see a
known-present variable before any absence is read.

A dispatch refused on follower conditions names every unmet condition in one
result, together with the one follower command that clears them. Reported one
at a time — no producer, then a follower whose lines reach a file, then a
session with no registration at all — the conditions cost one round trip each;
measured at about twenty-five minutes for a single worker. Judging the
session-side conditions in one pass must not widen a launch kind that carries
no session delivery: the producer is a condition of every kind, and a dispatch
under one of those kinds is still refused without it.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_watch as dispatch_watch_module
from reckon import cli, crew, crew_dispatch_commands, crew_follow_commands
from reckon.crew import runs

# ``reckon.crew`` re-exports a function named ``dispatch``, so the module a test
# patches is resolved through ``importlib`` rather than the package attribute.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The dispatch case arms a producer path and reads the arming variable, so this
# module is allowed to touch arming; the follower cases arm no producer.
pytestmark = [
    pytest.mark.arms_watch_producer,
    pytest.mark.xdist_group("follower_source"),
]

REPO_ROOT = Path(cli.__file__).resolve().parents[1]
FOLLOWER_SOURCE = Path(crew_follow_commands.__file__).resolve()
PROJECT = "proj"
# Distinctive on purpose: the shared-home check looks for these tokens, so an id
# short enough to appear inside a stranger's pointer would report a false hit.
SESSION = "s-follower-bounds"
RUN_ID = "r-follower-bounds"
NODE = "n1"

# The grant an arming carries and the slack each bound allows for the exit path.
# One poll interval is the follower's own pass rate (0.1 s) plus the cost of
# ending: releasing the registration, composing the final line, exiting.
LIFETIME = "6s"
ONE_POLL_INTERVAL_SLACK = 1.0
POLL_SECONDS = 0.05
ARM_WITHIN_SECONDS = 30.0
RELOAD_WITHIN_SECONDS = 10.0
# The injected clock of the carried-deadline case: one fixed step per read, so
# every interval it measures is a count of the follower's own clock reads. The
# replacement's setup is priced on that clock at a known interval, deliberately
# larger than the slack the bound allows, so an image that re-anchors lands past
# the bound by construction rather than by load.
CARRY_STEP = 0.05
SETUP_SECONDS = 2.0
# A poll stubbed to block past its lifetime: any value comfortably longer than
# the deadline will do, because the property under measure is that the follower
# does not wait for it.
STUB_BLOCK_SECONDS = 30.0
# The child-environment case waits for two recovery sweeps — the arm's and the
# replacement's — and a sweep's tail probes model lanes, so its grant must
# outlast the sweep rather than the case's bound: the property it measures is
# which variables a child inherits, not the deadline the reload case owns.
CHILD_LIFETIME = "25s"

# The replacement image is launched by the follower with a launcher that
# inserts its import root on ``sys.path`` before entering the command. That
# string is absent from the argv the test arms, so finding it on the process is
# direct evidence the image was replaced.
RELOAD_LAUNCHER_MARKER = "sys.path.insert"

# What the reload trigger appends to the source. A comment, and one whose every
# prefix is also a comment, so the module parses with the change in place.
_SOURCE_CHANGE_BYTES = b"\n# follower reload probe\n"

# The cases that force a source change edit one file on disk, and each restores
# what it read before editing. Two of them running at once can capture and write
# back each other's edit, leaving the append behind in the tree. One worker owns
# the group across every module that touches the source.
SOURCE_CHANGE_GROUP = "follower-source-change"


class _Clock:
    """A monotonic stand-in the test owns, so a deadline is an exact number."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class _StepClock:
    """A clock that advances one fixed step per read, so a case is exact.

    The interval between two reads is the number of reads in between, so an
    image that charges its setup to a grant ends that many steps later, and one
    that spends the instant it was handed ends within the reads of its exit
    path. Nothing here is timed from outside the process.
    """

    def __init__(self, start: float, step: float) -> None:
        self._t = float(start)
        self.step = float(step)

    def __call__(self) -> float:
        value = float(self._t)
        self._t += self.step
        return value

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def now(self) -> float:
        """The current reading, without spending a step to take it."""
        return self._t


class _InjectedTime:
    """The ``time`` module as ``reckon.cli`` sees it, with both clocks replaced.

    The command reads ``time.monotonic`` and ``time.time``, so both name the
    case's clock and every other attribute is the real module's. Only the
    module object the command resolves its own ``time`` through is swapped, so
    no other code in the process sees anything but real time.
    """

    def __init__(self, real, clock) -> None:
        self._real = real
        self.monotonic = clock
        self.time = clock

    def __getattr__(self, name: str):
        return getattr(self._real, name)


# ── A poll that outlives the deadline is abandoned, not waited for ──────────


def test_a_poll_that_outlives_the_deadline_is_abandoned() -> None:
    """The arming ends at its deadline while its poll is still blocked.

    The clock and the poll are injected, so the deadline is an exact instant
    and the poll is stubbed to block far past it. The follower must end within
    one second of the deadline, and the poll must still be blocked when it
    does: a follower that waited for the poll to return would end when the poll
    released it, which is what this case rules out.
    """
    clock = _Clock(1000.0)
    remaining = 0.5
    deadline = clock() + remaining
    stop = threading.Event()
    poll_started = threading.Event()
    poll_release = threading.Event()
    poll_returned = threading.Event()
    driven: list[list[dict]] = []
    failures: list[BaseException] = []

    def _blocking_poll(_project: str) -> dict:
        poll_started.set()
        poll_release.wait(STUB_BLOCK_SECONDS)
        poll_returned.set()
        return {"resumed": [], "skipped": []}

    def _drive() -> None:
        try:
            driven.append(
                list(
                    cli._follow_watch_lines(
                        PROJECT,
                        session=SESSION,
                        poll_interval=0.01,
                        sleeper=lambda _seconds: None,
                        stop=stop,
                        sweep=_blocking_poll,
                        clock=clock,
                        lifetime=30.0,
                        lifetime_deadline=deadline,
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001 - reported through this test's own failure
            failures.append(exc)

    started = time.monotonic()
    worker = threading.Thread(target=_drive, daemon=True)
    worker.start()
    try:
        assert poll_started.wait(ARM_WITHIN_SECONDS), (
            "the follower never ran a poll, so nothing about the deadline's "
            "bound on one was measured"
        )
        worker.join(remaining + ONE_POLL_INTERVAL_SLACK + 1.0)
        ended = time.monotonic()
        poll_still_blocked = not poll_returned.is_set()
    finally:
        stop.set()
        poll_release.set()
        worker.join(ARM_WITHIN_SECONDS)

    assert not failures, f"the follower raised out of its loop: {failures!r}"
    assert not worker.is_alive(), (
        "the follower was still running while its poll blocked past the "
        "deadline, so the deadline does not bound a poll"
    )
    assert poll_still_blocked, (
        "the poll returned before the follower ended, so this case measured a "
        "poll that finished rather than one that was abandoned"
    )
    assert any(event.get("event") == cli.FOLLOWER_END_EVENT for event in driven[0]), (
        f"the arming must end with its own final line; events={driven!r}"
    )
    overshoot = ended - started - remaining
    assert overshoot <= ONE_POLL_INTERVAL_SLACK, (
        f"the arming must end within {ONE_POLL_INTERVAL_SLACK!r}s of its "
        f"deadline; it ended {overshoot:.3f}s past the poll's remaining "
        f"lifetime of {remaining!r}s"
    )


# ── A reloaded follower ends within the original grant plus one poll ────────


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep registrations, pointers, and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every RECKON_ variable but the temporary home, for one case.

    The worker running this file may itself be under ``crew follow``, so its
    exported RECKON_ variables, inherited unchanged, would be read by the arming
    this case reconstructs as though it belonged to that session.
    """
    for key in [
        name
        for name in os.environ
        if name.startswith("RECKON_") and name != "RECKON_HOME"
    ]:
        monkeypatch.delenv(key, raising=False)


def _follower_env(home: Path, **extra: str) -> dict[str, str]:
    """An environment carrying only what this test sets deliberately.

    A worker whose own session runs under ``crew follow`` exports RECKON_
    variables describing *that* session, and inherited unchanged they make the
    follower this test arms believe it belongs to someone else's session. Every
    other RECKON_ variable is dropped, so the arming under measure is the one
    this file describes.
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("RECKON_")
    }
    env["RECKON_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(extra)
    return env


def _arm(home: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start the real follower command against the temporary config home."""
    return subprocess.Popen(
        [
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
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env if env is not None else _follower_env(home),
    )


def _kill(process: subprocess.Popen) -> tuple[str, str]:
    """End a follower the test is done with, and collect what it printed."""
    process.kill()
    stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
    return stdout, stderr


def _wait_until_armed(process: subprocess.Popen) -> None:
    """Wait for the follower to hold its registration, or fail saying which."""
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
            pytest.fail(
                f"the follower ended with exit status {process.returncode} "
                f"before it armed; stdout={stdout!r} stderr={stderr!r}"
            )
        if runs.follower_state(PROJECT, SESSION)["registered"] is True:
            return
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        f"the follower held no registration {ARM_WITHIN_SECONDS!r}s after it "
        f"started, so it never armed; stdout={stdout!r} stderr={stderr!r}"
    )


def _wait_until_looping(home: Path, pid: int) -> None:
    """Wait until the follower's stream loop has entered its first wait pass.

    The reloader fixes its source baseline partway through that entry, so a
    change made the moment the registration appears can be captured as the
    baseline itself and never seen as a change. The loop's first pass records a
    recovery status naming the sweeping pid, and no earlier pass can write it,
    so that record is the barrier.
    """
    status_path = home / "crew" / "recovery" / f"{PROJECT}.status.json"
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        try:
            record = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if record is not None and str(record.get("swept_by_pid")) == str(pid):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        "the follower never recorded a wait pass, so its source baseline was "
        "not known to be fixed before this case changed the source"
    )


def _process_argv(pid: int) -> str:
    """Read a live image's command line, or an empty string once it is gone."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _wait_until_reloaded(process: subprocess.Popen) -> None:
    """Wait for the replacement image, or fail saying the reload was not seen."""
    deadline = time.monotonic() + RELOAD_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if RELOAD_LAUNCHER_MARKER in _process_argv(process.pid):
            return
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        f"the follower did not replace its image within "
        f"{RELOAD_WITHIN_SECONDS!r}s of the source change, so the reload "
        f"survival was not exercised; argv={_process_argv(process.pid)!r} "
        f"stdout={stdout!r} stderr={stderr!r}"
    )


@contextlib.contextmanager
def _source_mutation_window():
    """Hold the one window in which a case may edit the source the follower stamps."""
    lock_path = Path(tempfile.gettempdir()) / "reckon-follower-source-change.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _force_source_change() -> tuple[bytes, int, int]:
    """Advance the follower's source stamp with a real content change."""
    stat = FOLLOWER_SOURCE.stat()
    original = FOLLOWER_SOURCE.read_bytes()
    with FOLLOWER_SOURCE.open("ab") as handle:
        handle.write(_SOURCE_CHANGE_BYTES)
    return original, stat.st_atime_ns, stat.st_mtime_ns


def _restore_source_bytes(restore: tuple[bytes, int, int]) -> None:
    """Put the source back byte for byte, and its timestamps back with it."""
    original, atime_ns, mtime_ns = restore
    FOLLOWER_SOURCE.write_bytes(original)
    os.utime(FOLLOWER_SOURCE, ns=(atime_ns, mtime_ns))


def test_a_reload_does_not_restart_the_lifetime(home, monkeypatch) -> None:
    """An image entering with a carried deadline spends what is left of it.

    This is a replacement's entry: the same command line the first arming had,
    with the absolute instant the re-exec hands over in the case's environment.
    The clock is injected and advances one step per read, and the setup a
    replacement pays before its wait loop is priced on that clock at a known
    interval, deliberately larger than the slack the bound allows. An image that
    re-anchors at its own start lands that whole interval past the bound by
    construction, where a wall-clock version of this case measured the
    replacement's setup and needed a slack wide enough to let it pass.
    """
    _clean_environ(monkeypatch)
    clock = _StepClock(1000.0, CARRY_STEP)
    grant = float(crew.parse_duration(LIFETIME))
    setup_readings: list[float] = []

    def _priced_setup() -> str:
        """A replacement image's setup, priced on this case's own clock."""
        setup_readings.append(clock.now())
        clock.advance(SETUP_SECONDS)
        return "stream"

    monkeypatch.setattr(runs, "delivery_mode", _priced_setup)
    real_lines = cli._follow_watch_lines

    def _injected_lines(*args, **kwargs):
        kwargs.setdefault("clock", clock)
        kwargs.setdefault("sleeper", lambda _seconds: None)
        return real_lines(*args, **kwargs)

    monkeypatch.setattr(crew_follow_commands, "_follow_watch_lines", _injected_lines)
    monkeypatch.setattr(crew_follow_commands, "time", _InjectedTime(time, clock))
    # The instant the re-exec of an armed follower carries, in the variable it
    # carries it in, so this case enters where a replacement image enters.
    monkeypatch.setenv(cli._FOLLOWER_LIFETIME_ENV, str(1000.0 + grant))

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--no-color",
            "--lifetime",
            LIFETIME,
        ],
    )

    assert setup_readings, (
        "the replacement's setup was never priced, so the cost a re-anchoring "
        "image charges to the grant was never measured"
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines and lines[-1].strip().startswith("follower end:"), (
        f"the arming must end with its own final line; got {lines!r}"
    )
    overshoot = clock.now() - (1000.0 + grant)
    assert overshoot <= ONE_POLL_INTERVAL_SLACK, (
        f"the carried instant must end the arming within "
        f"{ONE_POLL_INTERVAL_SLACK!r}s of itself, whatever the replacement pays "
        f"to start; it ended {overshoot:.3f}s past it, which is the "
        f"replacement's own setup charged to the grant"
    )


# ── No child of an armed follower carries the hand-off variables ────────────


def _probe_code(dump: Path) -> str:
    """The probe's program: record the environment the child was handed.

    It appends one comma-joined line of variable names per run, so a follower
    that sweeps more than once — before and after an image replacement — leaves
    a line from each image rather than overwriting the first.
    """
    return (
        "import os\n"
        f"open({str(dump)!r}, 'a', encoding='utf-8').write("
        "','.join(sorted(os.environ)) + '\\n')\n"
        "print('recorded')\n"
    )


def _write_parked_run(home: Path, dump: Path) -> None:
    """One live run parked on a wait whose probe records its own environment.

    The sweep runs a parked run's declared probe through ``subprocess.run``
    inheriting the follower's environment, so this is the follower's own
    subprocess path: whatever the probe's child inherits is what every child
    the follower starts would inherit. The terminal state is never printed, so
    the run stays parked and the probe runs again on the replacement image.
    """
    log = home / "logs" / f"{RUN_ID}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    worktree = home / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    # The declared probe must name something outside the worker — a path — or
    # the wait reader refuses it as a probe that cannot fail. The dump is the
    # file the probe writes, so it is created up front and named as the
    # reference the declaration reads.
    dump.touch()
    probe = [sys.executable, "-c", _probe_code(dump), str(dump)]
    manifest.write_text(
        "status: waiting\n"
        "wait_condition: a child's environment has been recorded\n"
        f"wait_probe: {json.dumps(probe)}\n"
        'wait_terminal: ["absent"]\n'
        "resume_brief: read the recorded environment and finish\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": NODE, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": runs._utc_now(),
            "worktree": str(worktree),
            "manifest_path": str(manifest),
            "log_path": str(log),
            "manifest_baseline_mtime_ns": manifest.stat().st_mtime_ns - 1_000_000_000,
        },
    )


def _recorded_environments(dump: Path) -> list[str]:
    if not dump.exists():
        return []
    return [
        line for line in dump.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _wait_for_child(dump: Path, process: subprocess.Popen) -> list[str]:
    """Wait for the follower's sweep to run the probe, or fail saying which."""
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        lines = _recorded_environments(dump)
        if lines:
            return lines
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        "the follower ran no probe child within "
        f"{ARM_WITHIN_SECONDS!r}s of its arm, so the probe that reports what a "
        f"child inherits was never run; stdout={stdout!r} stderr={stderr!r}"
    )


def _wait_for_more_children(
    dump: Path, observed: int, process: subprocess.Popen
) -> list[str]:
    """Wait for the replacement image's own probe child, or fail saying which.

    A dump holding only the first image's line is a clean report about one
    image, so the post-reload half waits for a line the replacement wrote
    rather than asserting over whatever is there when the deadline arrives.
    """
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        lines = _recorded_environments(dump)
        if len(lines) > observed:
            return lines
        if process.poll() is not None:
            break
        time.sleep(POLL_SECONDS)
    stdout, stderr = _kill(process)
    pytest.fail(
        f"the replacement image ran no probe child of its own within "
        f"{ARM_WITHIN_SECONDS!r}s of the reload, so the environment the second "
        f"image hands a child was never observed; "
        f"lines={_recorded_environments(dump)!r} stdout={stdout!r} "
        f"stderr={stderr!r}"
    )


@pytest.mark.xdist_group(SOURCE_CHANGE_GROUP)
def test_no_child_of_an_armed_follower_carries_the_hand_off_variables(home) -> None:
    """The checkpoint and the arming variable reach no child of the follower.

    Both are hand-offs to one process and both were placed in ``os.environ``,
    which every child of the follower inherits. The probe records the
    environment it was handed — the follower's own environment as a child sees
    it — and the test arms the follower with the arming variable set and forces
    a reload, so the checkpoint's hand-over path is exercised too. The
    instrument is proved to see a known-present variable (``RECKON_HOME``)
    before any absence is read.
    """
    dump = home / "child-environment.txt"
    _write_parked_run(home, dump)
    restore = None
    with _source_mutation_window():
        process = _arm(
            home,
            "--lifetime",
            CHILD_LIFETIME,
            env=_follower_env(home, RECKON_WATCH_ARMING="off"),
        )
        try:
            _wait_until_armed(process)
            arm_at = time.monotonic()
            lines = _wait_for_child(dump, process)
            # The instrument must be shown to see a known-present variable
            # before an absence means anything: the follower was armed with
            # RECKON_HOME, so a child inheriting its environment carries it.
            assert any("RECKON_HOME" in line for line in lines), (
                "the probe did not observe the environment it was handed, so "
                f"it cannot report what is absent from it; lines={lines!r}"
            )
            assert not any("RECKON_FOLLOWER_CHECKPOINT" in line for line in lines), (
                "a child the follower started inherited RECKON_FOLLOWER_CHECKPOINT"
            )
            assert not any("RECKON_WATCH_ARMING" in line for line in lines), (
                "a child the follower started inherited RECKON_WATCH_ARMING, so "
                f"the arming variable was left in its environment; lines={lines!r}"
            )

            _wait_until_looping(home, process.pid)
            restore = _force_source_change()
            _wait_until_reloaded(process)
            # The replacement image runs the sweep on its first wait pass, so
            # this half waits for a line the replacement itself wrote: a dump
            # holding only the first image's line is a clean report about one
            # image, and whether the second sweep lands before the deadline is
            # exactly the race this half must not depend on.
            _wait_for_more_children(dump, len(lines), process)
            deadline = (
                arm_at
                + float(crew.parse_duration(CHILD_LIFETIME))
                + (ONE_POLL_INTERVAL_SLACK)
            )
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(POLL_SECONDS)
            stdout, stderr = process.communicate(timeout=ARM_WITHIN_SECONDS)
        finally:
            if process.poll() is None:
                _kill(process)
            if restore is not None:
                _restore_source_bytes(restore)

    assert process.returncode == 0, (
        f"a lifetime exit is the follower ending by itself, so it exits zero; "
        f"got {process.returncode}; stderr={stderr!r}; stdout={stdout!r}"
    )
    lines = _recorded_environments(dump)
    assert any("RECKON_HOME" in line for line in lines), (
        f"the replacement image's probe did not observe its environment; lines={lines!r}"
    )
    assert not any("RECKON_FOLLOWER_CHECKPOINT" in line for line in lines), (
        "a child started after the reload inherited RECKON_FOLLOWER_CHECKPOINT, "
        f"so the checkpoint was placed in the follower's own os.environ; lines={lines!r}"
    )
    assert not any("RECKON_WATCH_ARMING" in line for line in lines), (
        f"a child started after the reload inherited RECKON_WATCH_ARMING; lines={lines!r}"
    )


# ── One refusal names every unmet follower condition ────────────────────────


CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

# One backend whose launch is not a session delivery, so the session-side
# follower conditions do not apply to it while the producer's absence still does.
IN_HARNESS_CONFIG = {
    "default_backend": "local",
    "backends": {
        "local": {
            "launch": "in-harness",
            "model": "fixture-model",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    """A mountable repository carrying the plan and the fleet script dispatch needs."""
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    source = REPO_ROOT / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    for arguments in (
        ["add", "seed.txt", "skills", "docs"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *args, **kwargs: CONFIG)


def _dispatch_arguments(repo: Path, session: str) -> list[str]:
    return [
        "crew",
        "dispatch",
        "--project",
        "sample",
        "--plan",
        "fixture",
        "--section",
        "guard",
        "--spec-level",
        "exact",
        "--node",
        "candidate",
        "--goal",
        "record dispatch admission for one session",
        "--done-when",
        "the command reports one refusal naming every unmet follower condition",
        "--write-path",
        "src/candidate.py",
        "--session",
        session,
        "--repo",
        str(repo),
    ]


def test_a_refusal_names_every_unmet_follower_condition_and_the_one_command(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unmet conditions are reported in one result, beside the follower command.

    The state is the one a coordinator met three refusals into: no producer
    left for the project, a follower of this session whose lines reach a file
    rather than a pane, and a peer session that does deliver. The refusal must
    name both of this session's unmet conditions, name the peer that is
    already covered, and carry the one follower command that clears them, in a
    single result — instead of surfacing one condition per dispatch.
    """
    project = "sample"
    session = "session-unmet"
    peer_session = "session-delivering"

    class _DeadSupervisor:
        """A producer that could not be started: the arming died at once."""

        def poll(self) -> int:
            return 1

    monkeypatch.setattr(
        dispatch_watch_module,
        "_start_watch_producer",
        lambda _project: _DeadSupervisor(),
    )

    with (
        runs.follower_registration(project, session, delivery="file"),
        runs.follower_registration(project, peer_session, delivery="stream"),
    ):
        result = CliRunner().invoke(cli.main, _dispatch_arguments(repo, session))

    assert result.exit_code == 8, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "watcher-required"
    detail = str(payload["detail"])
    assert "no live crew watcher process" in detail, (
        f"the producer condition must be named; detail={detail!r}"
    )
    assert "not delivering" in detail and "file" in detail, (
        f"the session's follower condition must be named; detail={detail!r}"
    )
    assert peer_session in detail, (
        f"the peer that does deliver must be named; detail={detail!r}"
    )
    attach = runs.watch_state(project, session=session)["attach_line"]
    assert f"`{attach}`" in detail or attach in detail, (
        f"the one required follower command must be quoted; detail={detail!r}"
    )
    assert not list(crew.list_live(project=project)), "nothing may be created"


def test_a_launch_that_carries_no_delivery_still_needs_the_producer(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch kind with no session delivery is still refused without a producer.

    An in-harness node prepares a directive rather than delivering lines to a
    session, so the session-side follower conditions do not apply to it — but
    the producer is a condition of every launch kind: the seat is what the
    project's watcher holds, and a launch was refused for its absence before
    the session-side conditions were judged in one pass. The session here is
    delivering, so the producer is the only condition left, and narrowing the
    refusal to the cli kind would admit this launch with nothing reading the
    project at all.
    """
    project = "sample"
    session = "session-in-harness"
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *args, **kwargs: IN_HARNESS_CONFIG
    )

    class _DeadSupervisor:
        """A producer that could not be started: the arming died at once."""

        def poll(self) -> int:
            return 1

    monkeypatch.setattr(
        dispatch_watch_module, "_start_watch_producer", lambda _project: _DeadSupervisor()
    )

    with runs.follower_registration(project, session, delivery="stream"):
        result = CliRunner().invoke(cli.main, _dispatch_arguments(repo, session))

    assert result.exit_code == 8, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "watcher-required"
    detail = str(payload["detail"])
    assert "no live crew watcher process" in detail, (
        f"the producer condition must be named for this launch kind too; "
        f"detail={detail!r}"
    )
    assert not list(crew.list_live(project=project)), "nothing may be created"
