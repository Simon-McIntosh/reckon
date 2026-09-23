"""A follower whose consumer is gone releases its registration and exits silently.

A follower delivers to the session that armed it, and it holds an advisory lock
on that session's registration so a dispatch can prove a reader exists. When the
consuming process dies, the follower does not: it is reparented to init and
keeps the lock, so the session's next follower streams read-only and every
dispatch from the session is refused ``watcher-required`` until someone kills
the orphan by hand. A follower with no consumer delivers to nobody, so holding
the registration serves no reader.

These tests run the real command against a temporary crew config home, so the
end of the journey is the process leaving and its registration free. The
follower is started by an intermediate parent the test can kill on its own: the
recorded consumer is that parent, and killing it is what reparents the follower
without also killing the test. The follower's stdout is a file so "wrote nothing
after the kill" is read rather than inferred.

Two tests share the arming and the temporary home, and they split on what the
consumer does. The release test kills the intermediate parent, so the follower's
recorded consumer is gone: within ten seconds the follower leaves, its
registration is free, and the next follower is the holder. The control test
leaves the parent alive, so the recorded consumer is still reading: past that
same ten seconds the follower is still running and still the holder, because the
check that ends an orphan must not end a follower whose lines reach someone.

The gate declares one negative control: with the consumer check forced to report
the consumer gone however the parent is doing, every follower releases, so the
live-consumer test fails while the release test still passes.
"""

from __future__ import annotations

import contextlib
import fcntl
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

# A project name this workstation does not use, so the real follower directory
# for it is absent and the untouched assertion is a control rather than a
# coincidence of an existing empty directory.
PROJECT = "orphan-release-probe"
SESSION = "s1"

# Within ten seconds of the kill, the orphan has exited, freed the
# registration, and the next follower holds it. Reaching an arm costs a few
# seconds of interpreter and import latency, so the same window covers the
# second follower's arrival.
RELEASE_WITHIN_SECONDS = 10.0
# Reaching an arm and starting a second follower each cost a few seconds of
# interpreter and import latency, which is arrival rather than the release the
# ten-second window bounds.
ARM_WITHIN_SECONDS = 30.0
POLL_SECONDS = 0.05

# The intermediate parent starts the follower and then waits here. It exists to
# be killed: the follower's recorded consumer is this process, so the test can
# end the consumer without ending itself.
_INTERMEDIATE = """
import os, subprocess, sys, time

stdout_path, stderr_path, pid_path = sys.argv[1], sys.argv[2], sys.argv[3]
out = open(stdout_path, "w", encoding="utf-8")
err = open(stderr_path, "w", encoding="utf-8")
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
    cwd=os.environ["FOLLOWER_TEST_ROOT"],
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


def _start_intermediate(
    home: Path, workdir: Path
) -> tuple[subprocess.Popen, Path, Path, Path]:
    """Start the process that owns the follower and can be killed on its own."""
    stdout_path = workdir / "follower.stdout"
    stderr_path = workdir / "follower.stderr"
    pid_path = workdir / "follower.pid"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _INTERMEDIATE,
            str(stdout_path),
            str(stderr_path),
            str(pid_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "RECKON_HOME": str(home),
            "PYTHONPATH": str(REPO_ROOT),
            "FOLLOWER_TEST_PROJECT": PROJECT,
            "FOLLOWER_TEST_SESSION": SESSION,
            "FOLLOWER_TEST_ROOT": str(REPO_ROOT),
        },
    )
    return process, stdout_path, stderr_path, pid_path


def _follower_pid(pid_path: Path, intermediate: subprocess.Popen) -> int:
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if intermediate.poll() is not None:
            _stderr = intermediate.communicate(timeout=RELEASE_WITHIN_SECONDS)[1]
            pytest.fail(
                "the intermediate parent exited before it started the follower; "
                f"stderr={_stderr!r}"
            )
        if pid_path.is_file():
            text = pid_path.read_text().strip()
            if text.isdigit():
                return int(text)
        time.sleep(POLL_SECONDS)
    pytest.fail("the intermediate parent never recorded a follower pid")


def _wait_until_armed(follower_pid: int, intermediate: subprocess.Popen) -> None:
    """Wait until the follower holds its registration, or say it never armed."""
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if intermediate.poll() is not None:
            _stderr = intermediate.communicate(timeout=RELEASE_WITHIN_SECONDS)[1]
            pytest.fail(
                "the intermediate parent exited before the follower armed; "
                f"stderr={_stderr!r}"
            )
        state = runs.follower_state(PROJECT, SESSION)
        if state["registered"] is True and str(state["follower"].get("pid")) == str(
            follower_pid
        ):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        "the follower never armed a registration for its session before the "
        "consumer was killed, so the release this test measures was not reached"
    )


def _consumer_gone(follower_pid: int, start_time: str | None) -> bool:
    """Whether the follower process has left the process table."""
    if runs._process_start_time(follower_pid) != start_time:
        return True
    return runs.process_alive(follower_pid) is not True


def _wait_until_released(
    follower_pid: int, start_time: str | None, deadline: float
) -> None:
    while time.monotonic() < deadline:
        if _consumer_gone(follower_pid, start_time):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"the orphaned follower was still running {RELEASE_WITHIN_SECONDS!r}s after "
        "its consumer was killed, so it did not release and leave"
    )


def _assert_registration_free() -> None:
    """Prove the registration is unlocked, by taking the lock non-blocking."""
    path = runs.follower_lock_path(PROJECT, SESSION)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail(
                "the session's registration is still locked after the orphan "
                "left, so the next follower cannot take it"
            )
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _assert_registration_locked() -> None:
    """Prove the registration is still held, by a non-blocking acquire failing."""
    path = runs.follower_lock_path(PROJECT, SESSION)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            pytest.fail(
                "a follower whose consumer is still reading must keep the "
                "registration locked, so a non-blocking acquire must fail"
            )


def _holds_through_release_window(
    follower_pid: int, start_time: str | None, intermediate: subprocess.Popen
) -> None:
    """Require the follower to survive the window an orphan releases within.

    The window is the release case's: a follower that has lost its consumer
    must leave within ten seconds. Here the consumer is alive throughout, so
    the check must not fire and the follower must still be running at the far
    end of that same window.
    """
    deadline = time.monotonic() + RELEASE_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if intermediate.poll() is not None:
            _stderr = intermediate.communicate(timeout=RELEASE_WITHIN_SECONDS)[1]
            pytest.fail(
                "the intermediate parent exited on its own while it was meant "
                f"to stay the follower's live consumer; stderr={_stderr!r}"
            )
        if _consumer_gone(follower_pid, start_time):
            pytest.fail(
                "a follower whose consumer is still reading must not release: "
                f"it left inside the {RELEASE_WITHIN_SECONDS!r}s window in which "
                "an orphaned follower releases"
            )
        time.sleep(POLL_SECONDS)


def _start_second_follower(home: Path) -> subprocess.Popen:
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "RECKON_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)},
    )


def _wait_until_holder(second: subprocess.Popen, deadline: float) -> None:
    while time.monotonic() < deadline:
        if second.poll() is not None:
            _stderr = second.communicate(timeout=RELEASE_WITHIN_SECONDS)[1]
            pytest.fail(
                f"the second follower exited instead of taking the registration; "
                f"stderr={_stderr!r}"
            )
        state = runs.follower_state(PROJECT, SESSION)
        if state["registered"] is True and str(state["follower"].get("pid")) == str(
            second.pid
        ):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        "the second follower did not become the registered holder after the "
        "orphan released, so the release did not free the registration"
    )


def _kill(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=RELEASE_WITHIN_SECONDS)


def test_an_orphaned_follower_releases_its_registration_and_leaves(home) -> None:
    """Killing the consumer frees the registration and ends the follower.

    The consumer is the intermediate parent this test starts. Killing it with
    SIGKILL reparents the follower to init, which is the state the plan
    measures: the recorded parent no longer names the process the follower
    reports to. Within ten seconds of that kill the follower has left, its
    stdout carries nothing written after the kill, a non-blocking acquire takes
    the session's registration, and a freshly started follower is the holder
    named by ``follower_state``.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    intermediate: subprocess.Popen | None = None
    second: subprocess.Popen | None = None
    follower_pid: int | None = None
    start_time: str | None = None
    try:
        intermediate, stdout_path, _stderr_path, pid_path = _start_intermediate(
            home, home
        )
        follower_pid = _follower_pid(pid_path, intermediate)
        _wait_until_armed(follower_pid, intermediate)
        start_time = runs._process_start_time(follower_pid)

        intermediate.kill()
        intermediate.communicate(timeout=RELEASE_WITHIN_SECONDS)
        deadline = time.monotonic() + RELEASE_WITHIN_SECONDS

        _wait_until_released(follower_pid, start_time, deadline)

        written = stdout_path.read_text()
        assert written == "", (
            "the release path is silent: nobody is left to read a line, so the "
            f"follower's stdout must carry nothing; got {written!r}"
        )

        _assert_registration_free()

        second = _start_second_follower(home)
        _wait_until_holder(second, deadline)
    finally:
        _kill(second)
        _kill(intermediate)
        if follower_pid is not None and not _consumer_gone(follower_pid, start_time):
            with contextlib.suppress(ProcessLookupError):
                os.kill(follower_pid, 9)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )


def test_a_follower_whose_consumer_reads_keeps_its_registration(home) -> None:
    """A follower whose consumer is still reading holds through the window.

    The release case's control: the same arming, with the intermediate parent
    left alive so the follower's recorded consumer is still reading. Past the
    ten seconds in which an orphaned follower releases and leaves, this
    follower is still running, ``follower_state`` still names it as the holder,
    and its registration is still locked. It separates a follower that holds the
    lock because its consumer is gone from one that holds it because its
    consumer is there.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    intermediate: subprocess.Popen | None = None
    follower_pid: int | None = None
    start_time: str | None = None
    try:
        intermediate, _stdout_path, _stderr_path, pid_path = _start_intermediate(
            home, home
        )
        follower_pid = _follower_pid(pid_path, intermediate)
        _wait_until_armed(follower_pid, intermediate)
        start_time = runs._process_start_time(follower_pid)

        _holds_through_release_window(follower_pid, start_time, intermediate)

        state = runs.follower_state(PROJECT, SESSION)
        assert state["registered"] is True, (
            "a follower whose consumer is still reading must stay registered"
        )
        assert str(state["follower"].get("pid")) == str(follower_pid), (
            "the live follower must remain the holder named by follower_state"
        )
        _assert_registration_locked()
    finally:
        _kill(intermediate)
        if follower_pid is not None and not _consumer_gone(follower_pid, start_time):
            with contextlib.suppress(ProcessLookupError):
                os.kill(follower_pid, 9)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )
