"""A second pytest session on a held basetemp is refused before it removes anything.

At startup pytest removes and recreates an explicit ``--basetemp``, and this
suite's session reaper then signals every crew watch producer whose home lies
under that root. So a second session given the same basetemp deletes the first
session's in-flight fixtures, and a dispatch then refuses for a missing
watcher. The session lock in ``tests/conftest.py`` admits one holder at a time.

Each case drives real child pytest sessions whose shared basetemp is a
temporary directory outside any repository: the sessions run a generated
driver module with the repository's conftest loaded as a plugin, so the lock is
exercised by the same code path a worker's gate takes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# A driver module the child sessions run. It records the ``tmp_path`` it was
# given and writes one fixture file, then — when asked — holds the session open
# on a release file so the parent can start a second session against the same
# basetemp while this one is demonstrably alive.
_DRIVER_SOURCE = """\
import os
import pathlib
import time


def test_session_holds_its_basetemp(tmp_path):
    # The fixture file is named for the pid that wrote it: a second session
    # that removes and recreates this basetemp would rebuild the same numbered
    # directory, so a fixed name and content could not tell that apart from the
    # first session's file having survived.
    (tmp_path / ("fixture-" + str(os.getpid()) + ".txt")).write_text(
        "kept", encoding="utf-8"
    )
    marker = os.environ.get("BASETEMP_SESSION_MARKER")
    if marker:
        pathlib.Path(marker).write_text(str(tmp_path), encoding="utf-8")
    release = os.environ.get("BASETEMP_SESSION_RELEASE")
    if release:
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if pathlib.Path(release).exists():
                break
            time.sleep(0.02)
"""

_HOLD_TIMEOUT = 30.0


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PACKAGE_ROOT), env.get("PYTHONPATH", "")) if part
    )
    # A dispatch identity is a fact about the worker that ran the parent suite,
    # not about these children; the lock under test does not read it.
    for name in ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT"):
        env.pop(name, None)
    return env


def _driver(root: Path) -> Path:
    driver = root / "test_shared_basetemp_driver.py"
    driver.write_text(_DRIVER_SOURCE, encoding="utf-8")
    return driver


def _start_session(root: Path, driver: Path, basetemp: Path, **extra_env):
    env = _child_env()
    env.update(extra_env)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.conftest",
            "--basetemp",
            str(basetemp),
            "-q",
            str(driver),
        ],
        cwd=str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_session(
    root: Path, driver: Path, basetemp: Path, **extra_env
) -> subprocess.CompletedProcess:
    session = _start_session(root, driver, basetemp, **extra_env)
    out, _ = session.communicate(timeout=_HOLD_TIMEOUT * 2)
    return subprocess.CompletedProcess(session.args, session.returncode, out, "")


def _session_output(session: subprocess.CompletedProcess) -> str:
    return session.stdout + session.stderr


def _wait_for(predicate, timeout: float = _HOLD_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _lock_path(basetemp: Path) -> Path:
    return basetemp.parent / (basetemp.name + ".lock")


def _lock_pid(basetemp: Path) -> int | None:
    try:
        value = json.loads(_lock_path(basetemp).read_text() or "{}")
    except (OSError, ValueError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) else None


def _process_start_time(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``: the process's start time in clock ticks."""
    try:
        stat = Path("/proc", str(pid), "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        fields = stat[stat.rindex(")") + 2 :].split()
    except ValueError:
        return None
    try:
        return int(fields[22 - 3])
    except (IndexError, ValueError):
        return None


def _reap(session: subprocess.Popen) -> str:
    if session.poll() is None:
        session.terminate()
    try:
        out, _ = session.communicate(timeout=_HOLD_TIMEOUT)
    except subprocess.TimeoutExpired:
        session.kill()
        out, _ = session.communicate(timeout=_HOLD_TIMEOUT)
    return out


def _await_exit(session: subprocess.Popen) -> None:
    """Let ``session`` finish on its own, forcing it only if it will not."""
    try:
        session.communicate(timeout=_HOLD_TIMEOUT)
    except subprocess.TimeoutExpired:
        _reap(session)


def test_a_second_session_is_refused_and_the_first_keeps_its_fixtures(tmp_path):
    root = tmp_path / "case1"
    root.mkdir()
    basetemp = root / "shared-basetemp"
    driver = _driver(root)
    held = root / "held"
    released = root / "released"

    first = _start_session(
        root,
        driver,
        basetemp,
        BASETEMP_SESSION_MARKER=str(held),
        BASETEMP_SESSION_RELEASE=str(released),
    )
    try:
        # The marker proves the first session is mid-run with a fixture
        # directory under the basetemp; it writes it long after the lock is
        # taken, at configure time.
        assert _wait_for(held.exists), "the first session never wrote its tmp_path"
        held_tmp = Path(held.read_text(encoding="utf-8"))
        held_fixture = held_tmp / f"fixture-{first.pid}.txt"
        assert held_fixture.read_text(encoding="utf-8") == "kept"

        second = _run_session(root, driver, basetemp)
        output = _session_output(second)

        assert held_fixture.read_text(encoding="utf-8") == "kept", (
            "the second session removed the first session's fixture directory:\n"
            + output
        )
        assert second.returncode != 0, (
            "the second session on a held basetemp was not refused:\n" + output
        )
        assert str(first.pid) in output, (
            "the refusal does not name the holding pid:\n" + output
        )
        assert str(basetemp) in output, (
            "the refusal does not name the basetemp:\n" + output
        )
        assert first.poll() is None, "the first session did not survive the refusal"
        assert _lock_pid(basetemp) == first.pid, (
            "the first session does not hold the basetemp lock it took"
        )
    finally:
        released.write_text("release", encoding="utf-8")
        _await_exit(first)

    assert not _lock_path(basetemp).exists(), (
        "the first session did not release the lock when it exited"
    )


def test_a_sequential_session_on_the_same_basetemp_starts_normally(tmp_path):
    root = tmp_path / "case2"
    root.mkdir()
    basetemp = root / "shared-basetemp"
    driver = _driver(root)

    first = _run_session(root, driver, basetemp)
    assert first.returncode == 0, "the first session did not run:\n" + first.stdout
    assert not _lock_path(basetemp).exists(), (
        "the first session left its lock behind on a clean exit"
    )

    second = _run_session(root, driver, basetemp)
    assert second.returncode == 0, (
        "a sequential session on a released basetemp was refused:\n" + second.stdout
    )
    assert "1 passed" in second.stdout


def test_a_lock_left_by_a_dead_pid_is_taken_over(tmp_path):
    root = tmp_path / "case3"
    root.mkdir()
    basetemp = root / "shared-basetemp"
    driver = _driver(root)

    crashed = _start_session(
        root, driver, basetemp, BASETEMP_SESSION_RELEASE=str(root / "never")
    )
    try:
        assert _wait_for(lambda: _lock_pid(basetemp) == crashed.pid), (
            "the first session never took the basetemp lock"
        )
        crashed.kill()
        crashed.wait(timeout=_HOLD_TIMEOUT)
        assert _lock_pid(basetemp) == crashed.pid, (
            "the killed session's lock was removed rather than left stale"
        )

        revived = _run_session(root, driver, basetemp)
        assert revived.returncode == 0, (
            "a session did not take over a lock left by a dead pid:\n" + revived.stdout
        )
        assert "1 passed" in revived.stdout
    finally:
        _reap(crashed)


def test_a_lock_naming_a_reused_pid_is_taken_over(tmp_path):
    root = tmp_path / "case4"
    root.mkdir()
    basetemp = root / "shared-basetemp"
    driver = _driver(root)

    # A lock left by a session that has since exited, whose number an unrelated
    # live process now carries: the pid is alive, but the start time recorded
    # beside it is not that process's own, so the lock is stale and must be
    # taken over. This process supplies the live pid, and its own start time
    # shifted by one clock tick stands in for the earlier session's.
    live_start = _process_start_time(os.getpid())
    assert live_start is not None, "could not read this process's start time"
    _lock_path(basetemp).write_text(
        json.dumps(
            {"pid": os.getpid(), "start": live_start + 1, "basetemp": str(basetemp)}
        ),
        encoding="utf-8",
    )

    session = _run_session(root, driver, basetemp)
    assert session.returncode == 0, (
        "a session refused a lock whose pid an unrelated process had reused:\n"
        + session.stdout
    )
    assert "1 passed" in session.stdout


def test_a_session_meeting_a_partial_lock_write_is_refused(tmp_path):
    root = tmp_path / "case5"
    root.mkdir()
    basetemp = root / "shared-basetemp"
    driver = _driver(root)

    # The state a first session's acquisition passes through when its lock is
    # created before its holder's record is written: the file exists and is
    # empty. It is held in that state rather than raced for, so the case is
    # deterministic. A second session must be refused, not unlink the
    # unreadable lock and take it. The mtime is refreshed while the second
    # session starts, so a lock that is genuinely mid-write stays younger than
    # the guard's grace whatever the host's load.
    lock = _lock_path(basetemp)
    handle = lock.open("w", encoding="utf-8")
    session = _start_session(root, driver, basetemp)
    try:
        deadline = time.monotonic() + _HOLD_TIMEOUT * 2
        while session.poll() is None and time.monotonic() < deadline:
            os.utime(handle.name, None)
            time.sleep(0.05)
        out, _ = session.communicate(timeout=_HOLD_TIMEOUT)
    finally:
        handle.close()
        _reap(session)
    assert session.returncode != 0, (
        "a session met a partial lock write and was not refused:\n" + out
    )
