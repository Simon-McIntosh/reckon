"""A follower checks its owner on every wait pass, holding the lock or not.

A follower delivers to the process that armed it, and it holds an advisory lock
on that session's registration so a dispatch can prove a reader exists. The
registration is per session, so a second follower armed while the first still
holds streams read-only. The check that ends a follower whose consumer is gone
ran only for the holder: it returned at once for a follower that did not hold
the registration, so a read-only follower never checked its owner at all. Every
Monitor expiry re-armed while an older follower held the lock added one more
read-only follower that never exited.

The owner is fixed once, at the follower's first start, as the pid and kernel
start time of the process that armed it, and carried as ``RECKON_FOLLOWER_OWNER``
rather than re-derived from ``os.getppid()`` — which names init or a subreaper
once the owner dies. The registration record cannot carry it either: a second
follower that takes a registration over rewrites that record from its own
parent, so a read-only follower under init would become a holder naming init as
its consumer and never leave. ``acquire()`` therefore records the armed owner,
and a recorded owner of pid 1 or below is gone rather than a reason to skip.

The read-only follower is measured with a *different* owner still holding the
lock, because the single-owner form masks the check: when one owner is killed,
the holder leaves and frees the registration, and the read-only follower exits
by taking that registration over rather than by checking its owner. Under the
old early return it would still exit there, so that form cannot show the defect.
The orphan's real condition is a read-only follower whose own owner is gone while
an older follower still holds the registration — one re-armed Monitor behind
another — and that is the condition this file sets up.

All cases use the real follower command against a temporary crew config home and
a stub process the test can kill on its own, so killing it ends the owner without
ending the test. Liveness is read from the process table rather than inferred.

The gate declares one negative control, executed and logged: restoring the early
return in ``_check_consumer`` for a non-holder, and making ``acquire()`` record
``os.getppid()`` again, must fail this file.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import cli
from reckon.crew import runs

REPO_ROOT = Path(cli.__file__).resolve().parents[1]
FOLLOWER_SOURCE = Path(cli.__file__).resolve()

# A project name this workstation does not use, so the real follower directory
# for it is absent and the untouched assertion is a control rather than a
# coincidence of an existing empty directory.
PROJECT = "follower-owner-probe"
SESSION = "s1"
OWNER_ENV = "RECKON_FOLLOWER_OWNER"

# Reaching an arm costs a few seconds of interpreter and import latency.
ARM_WITHIN_SECONDS = 30.0
# The exit a dead owner triggers runs on the follower's wait pass, whose cadence
# is a fraction of a second; five seconds is many passes plus the schedule jitter
# of a loaded login node. The live-owner control shows the same window is long
# enough to be meaningful: a follower which never checked would still be running
# at its end.
EXIT_WITHIN_SECONDS = 5.0
POLL_SECONDS = 0.05
RELOAD_WITHIN_SECONDS = 8.0

# The replacement image is launched with a launcher that inserts its import root
# on ``sys.path`` before entering the command. That string is absent from the
# argv the test arms, so finding it on the process is direct evidence that the
# image was replaced.
RELOAD_LAUNCHER_MARKER = "sys.path.insert"

# The stub process stands for one arming session. It starts one follower and
# then waits to be killed, so the follower's owner is a real parent the test can
# end on its own.
_STUB = """
import os, subprocess, sys, time

root, out_path, err_path, pid_path = sys.argv[1:5]
out = open(out_path, "w", encoding="utf-8")
err = open(err_path, "w", encoding="utf-8")
proc = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        "follow",
        "--project",
        os.environ["FOLLOWER_TEST_PROJECT"],
        "--session",
        os.environ["FOLLOWER_TEST_SESSION"],
        "--no-color",
    ],
    stdout=out,
    stderr=err,
    cwd=root,
)
with open(pid_path, "w", encoding="utf-8") as handle:
    handle.write(str(proc.pid))
time.sleep(600)
"""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep registrations, pointers, and streams in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _real_follower_dir(project: str) -> Path:
    """The follower directory this project resolves to outside a temp home."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", project).strip("-") or "project"
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    return (
        Path.home()
        / ".config"
        / "reckon"
        / "crew"
        / "watch"
        / f"{readable}-{digest}.followers"
    )


def _tree(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def _stub_env(home: Path) -> dict[str, str]:
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    environment.pop(OWNER_ENV, None)
    environment["RECKON_HOME"] = str(home)
    environment["FOLLOWER_TEST_PROJECT"] = PROJECT
    environment["FOLLOWER_TEST_SESSION"] = SESSION
    environment["FOLLOWER_TEST_ROOT"] = str(REPO_ROOT)
    return environment


def _start_stub(home: Path, workdir: Path, tag: str) -> tuple[subprocess.Popen, Path]:
    """Start an owner stub, which arms one follower and waits to be killed.

    Returns the stub and the path its follower's pid lands in. The follower's
    owner is this stub through ``os.getppid()``, so no owner is stamped in the
    environment: a process that arms a follower directly is treated as that
    follower's owner without extra cooperation.
    """
    pid_path = workdir / f"{tag}.pid"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _STUB,
            str(REPO_ROOT),
            str(workdir / f"{tag}.stdout"),
            str(workdir / f"{tag}.stderr"),
            str(pid_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=_stub_env(home),
    )
    return process, pid_path


def _arm(home: Path, owner: tuple[int, str] | None) -> subprocess.Popen:
    """Start a follower armed with an explicit owner identity."""
    environment = _stub_env(home)
    if owner is not None:
        environment[OWNER_ENV] = runs._format_follower_owner(owner)
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
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def _owner_identity(owner: subprocess.Popen) -> tuple[int, str]:
    start = runs._process_start_time(owner.pid)
    assert start, "the stub owner has no readable start time"
    return owner.pid, start


def _kill(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=EXIT_WITHIN_SECONDS)


def _kill_and_reap(process: subprocess.Popen) -> None:
    """Kill the stub and reap it, so its pid stops reading as alive."""
    process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=EXIT_WITHIN_SECONDS)


def _exited(pid: int) -> bool:
    """Whether a follower process has left the process table."""
    return runs.process_alive(pid) is not True


def _follower_pid(pid_path: Path, stub: subprocess.Popen, tag: str) -> int:
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if stub.poll() is not None:
            _stdout, stderr = stub.communicate(timeout=EXIT_WITHIN_SECONDS)
            pytest.fail(
                f"{tag}: the stub owner exited before it armed its follower; "
                f"stderr={stderr!r}"
            )
        if pid_path.is_file():
            text = pid_path.read_text().strip()
            if text.isdigit():
                return int(text)
        time.sleep(POLL_SECONDS)
    pytest.fail(f"{tag}: the stub owner never recorded a follower pid")


def _wait_until_holder(pid: int, *, deadline: float, tag: str) -> None:
    """Wait for this follower to hold the session's registration."""
    while time.monotonic() < deadline:
        if _exited(pid):
            pytest.fail(f"{tag}: the follower ended before it held the registration")
        state = runs.follower_state(PROJECT, SESSION)
        if state["registered"] is True and str(state["follower"].get("pid")) == str(
            pid
        ):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"{tag}: the follower never became the registered holder, so the arming "
        "under test was not reached"
    )


def _wait_until_read_only(
    pid: int, holder_pid: int, *, deadline: float, tag: str
) -> None:
    """Wait for a second follower to be live and streaming read-only."""
    while time.monotonic() < deadline:
        if _exited(pid):
            pytest.fail(f"{tag}: the read-only follower ended before it was observed")
        state = runs.follower_state(PROJECT, SESSION)
        held = str(state["follower"].get("pid"))
        if state["registered"] is True and held == str(holder_pid) and held != str(pid):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"{tag}: the follower never settled into a live, read-only reader while "
        f"{holder_pid} held the registration"
    )


def _wait_until_taken_over(pid: int, *, deadline: float) -> dict:
    """Wait for a read-only follower to take a released registration over."""
    while time.monotonic() < deadline:
        if _exited(pid):
            pytest.fail(
                "the read-only follower ended instead of taking the released "
                "registration over"
            )
        state = runs.follower_state(PROJECT, SESSION)
        if state["registered"] is True and str(state["follower"].get("pid")) == str(
            pid
        ):
            return dict(state["follower"])
        time.sleep(POLL_SECONDS)
    pytest.fail(
        "the read-only follower did not become the holder after the previous "
        "holder was killed, so no takeover record was written"
    )


def _wait_until_exited(pid: int, *, tag: str) -> float:
    """Wait for the follower to leave on its own; return how long it took.

    Never killed to make it so: the property under measure is that a dead owner
    ends the follower, and a killed process would answer a different question.
    The clock starts once the caller has observed the owner's death, so nothing
    about killing the owner is inside the window being bounded.
    """
    started = time.monotonic()
    while time.monotonic() - started < EXIT_WITHIN_SECONDS:
        if _exited(pid):
            return time.monotonic() - started
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"{tag}: the follower was still running {EXIT_WITHIN_SECONDS!r}s after "
        "its owner was killed, so it did not check its owner"
    )


def _process_argv(pid: int) -> str:
    """Read a live image's command line, or an empty string once it is gone."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _wait_until_reloaded(pid: int, *, tag: str) -> None:
    """Wait for the replacement image, identified by its own launcher.

    The replacement's command line carries the launcher that inserts the import
    root, which the armed argv does not. An image replacement is the only thing
    that can put it there, so this is the reload rather than a proxy.
    """
    deadline = time.monotonic() + RELOAD_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if RELOAD_LAUNCHER_MARKER in _process_argv(pid):
            return
        if _exited(pid):
            break
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"{tag}: the follower did not replace its image, so the owner's survival "
        f"across a reload was not exercised; argv={_process_argv(pid)!r}"
    )


def _force_source_change() -> tuple[int, int]:
    """Advance the follower's source stamp, and hand back what to restore."""
    stat = FOLLOWER_SOURCE.stat()
    now = time.time()
    os.utime(FOLLOWER_SOURCE, (now, now))
    return stat.st_atime_ns, stat.st_mtime_ns


def _restore_source_times(times: tuple[int, int]) -> None:
    os.utime(FOLLOWER_SOURCE, ns=(times[0], times[1]))


def test_a_read_only_follower_leaves_when_its_own_owner_dies(home) -> None:
    """A read-only follower leaves on the tick its own owner dies.

    Two owner stubs arm two followers for one session. The first follower takes
    the registration; the second streams read-only behind it. The read-only
    follower's owner is now killed while the first follower's owner stays alive
    and keeps the registration, so the read-only follower cannot leave by taking
    the registration over: the only thing that can end it is the check that used
    to return before it ran. The holder's own owner is then killed, and the
    holder leaves too, so both halves of the path are measured.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    holder_owner: subprocess.Popen | None = None
    reader_owner: subprocess.Popen | None = None
    holder_pid: int | None = None
    reader_pid: int | None = None
    try:
        holder_owner, holder_pid_path = _start_stub(home, home, "holder")
        holder_pid = _follower_pid(holder_pid_path, holder_owner, "holder")
        _wait_until_holder(
            holder_pid, deadline=time.monotonic() + ARM_WITHIN_SECONDS, tag="holder"
        )

        reader_owner, reader_pid_path = _start_stub(home, home, "reader")
        reader_pid = _follower_pid(reader_pid_path, reader_owner, "reader")
        _wait_until_read_only(
            reader_pid,
            holder_pid,
            deadline=time.monotonic() + ARM_WITHIN_SECONDS,
            tag="reader",
        )

        _kill_and_reap(reader_owner)
        read_only_elapsed = _wait_until_exited(reader_pid, tag="read-only follower")

        # The holder's owner was alive throughout, so the holder is still the
        # holder: the read-only follower left by checking its owner, not by
        # taking a registration that nothing had released.
        held = runs.follower_state(PROJECT, SESSION)["follower"]
        assert str(held.get("pid")) == str(holder_pid), (
            "the registration must still name the holder after the read-only "
            f"follower left; it names {held.get('pid')!r}"
        )

        _kill_and_reap(holder_owner)
        holder_elapsed = _wait_until_exited(holder_pid, tag="holder")
    finally:
        if holder_pid is not None and not _exited(holder_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(holder_pid, 9)
        if reader_pid is not None and not _exited(reader_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(reader_pid, 9)
        _kill(reader_owner)
        _kill(holder_owner)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )
    assert max(holder_elapsed, read_only_elapsed) <= EXIT_WITHIN_SECONDS


def test_a_reloaded_follower_still_leaves_when_the_original_owner_dies(home) -> None:
    """A follower that replaced its image still exits after the owner dies.

    The owner is fixed at the first arming and carried to the replacement image
    through the environment ``os.execve`` passes, so a reload continues the
    original arming rather than adopting whatever process replaced the image.
    The reload is forced through the same trigger the follower uses for a source
    change, and its occurrence is confirmed by the replacement's own command
    line, so a run in which no reload happened cannot pass by measuring the
    first image alone.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    owner: subprocess.Popen | None = None
    follower_pid: int | None = None
    restore: tuple[int, int] | None = None
    try:
        owner, pid_path = _start_stub(home, home, "reload")
        follower_pid = _follower_pid(pid_path, owner, "reload")
        _wait_until_holder(
            follower_pid, deadline=time.monotonic() + ARM_WITHIN_SECONDS, tag="reload"
        )

        restore = _force_source_change()
        _wait_until_reloaded(follower_pid, tag="reload")

        _kill_and_reap(owner)
        elapsed = _wait_until_exited(follower_pid, tag="reloaded follower")
    finally:
        if follower_pid is not None and not _exited(follower_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(follower_pid, 9)
        _kill(owner)
        if restore is not None:
            _restore_source_times(restore)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )
    assert elapsed <= EXIT_WITHIN_SECONDS


def test_a_takeover_records_the_armed_owner_not_the_claimants_parent(home) -> None:
    """A takeover keeps the original owner, and the owner's death ends it.

    The holding follower is killed, so the read-only follower takes the
    registration over. ``acquire()`` must record the process the follower was
    armed with rather than the claimant's current parent — which here is the
    test process, not the owner — so the taken-over record still names the
    owner. The owner then dies, and the follower that took the registration over
    leaves on the next tick.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    owner: subprocess.Popen | None = None
    holder: subprocess.Popen | None = None
    reader: subprocess.Popen | None = None
    try:
        owner = _start_owner_process()
        identity = _owner_identity(owner)
        armed_by = time.monotonic() + ARM_WITHIN_SECONDS

        holder = _arm(home, identity)
        _wait_until_holder(holder.pid, deadline=armed_by, tag="holder")
        _kill(holder)

        reader = _arm(home, identity)
        record = _wait_until_taken_over(reader.pid, deadline=armed_by)

        assert str(record.get("parent_pid")) == str(identity[0]), (
            "the taken-over record must name the process the follower was armed "
            f"with ({identity[0]}), not the claimant's current parent; the record "
            f"names {record.get('parent_pid')!r}"
        )
        assert record.get("parent_start_time") == identity[1], (
            "the taken-over record must carry the armed owner's start time; the "
            f"record carries {record.get('parent_start_time')!r}"
        )

        _kill_and_reap(owner)
        elapsed = _wait_until_exited(reader.pid, tag="takeover follower")
    finally:
        _kill(reader)
        _kill(holder)
        _kill(owner)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )
    assert elapsed <= EXIT_WITHIN_SECONDS


def test_a_follower_whose_owner_still_arms_it_keeps_reading(home) -> None:
    """A follower whose owner is alive keeps the registration through the window.

    The control for the exit cases: the same arming with the owner left alive.
    Past the window in which a follower whose owner died leaves, this one is
    still running and still the holder, so the bound above discriminates a
    follower that checked its owner from one that did not.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    owner: subprocess.Popen | None = None
    follower: subprocess.Popen | None = None
    try:
        owner = _start_owner_process()
        identity = _owner_identity(owner)
        follower = _arm(home, identity)
        _wait_until_holder(
            follower.pid, deadline=time.monotonic() + ARM_WITHIN_SECONDS, tag="control"
        )

        deadline = time.monotonic() + EXIT_WITHIN_SECONDS
        while time.monotonic() < deadline:
            if _exited(follower.pid):
                pytest.fail(
                    "a follower whose owner is still alive must not leave inside "
                    "the window in which an ownerless follower leaves"
                )
            state = runs.follower_state(PROJECT, SESSION)
            if state["registered"] is not True or str(
                state["follower"].get("pid")
            ) != str(follower.pid):
                pytest.fail(
                    "a follower whose owner is alive must stay the registered holder"
                )
            time.sleep(POLL_SECONDS)
    finally:
        _kill(follower)
        _kill(owner)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )


def _start_owner_process() -> subprocess.Popen:
    """Start a bare process that stands for an arming session, to be killed."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
