"""A follower with a reader of its stdout outlives the shell that armed it.

Two session followers cannot be written into one pipe in the foreground, so the
caller starts them as background jobs of a shell that exits at once::

    bash -c 'crew follow -p P -s A & crew follow -p P -s B &' | grep .

Both followers are re-parented as soon as that shell exits, while the pipe's
read end is still held open, and the owner check ended each on its next wait
pass — the reader saw end of file while both followers still had someone
reading. The rows reach that reader until it closes its end, which is the moment
a write would fail, so the check that ends an orphan must ask about the reader
rather than about the arming process alone.

Both cases run the real command against a temporary crew home, with the arming
owner stamped explicitly. On a node whose process is a child subreaper the
re-parented followers would hang from a live process and the owner-gone state
under test would never arise; stamping the arming shell's own pid, with no start
time, names exactly the process that exits.

The reader is a separate process appending each line it receives to a file, so a
row arriving there is read rather than inferred. The first case asserts the
first half: both followers are alive past the span, each still serving its own
session through a registration naming the process the shell started, so an
ending would break a session that still has a reader. The second closes the
reader's end and asserts the second half: both followers end and both
registrations are free.

The two cases together are what pins the ending to the reader rather than to the
arming process: after the shell has gone each follower must still be serving its
session, and the only event between the two cases is the reader's closure.

The negative control restores the owner-only check and must fail the first case.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import cli
from reckon.crew import runs
from reckon.crew_follow_commands import _stdout_has_reader

REPO_ROOT = Path(cli.__file__).resolve().parents[1]

# A project name this workstation does not use, so the real follower directory
# for it is absent and the untouched assertion is a control rather than a
# coincidence of an existing empty directory.
PROJECT = "follower-pipe-probe"
SESSION_A = "s-a"
SESSION_B = "s-b"
OWNER_ENV = "RECKON_FOLLOWER_OWNER"

# Reaching an arm costs a few seconds of interpreter and import latency.
ARM_WITHIN_SECONDS = 40.0
POLL_SECONDS = 0.05

# The span the followers must outlive. The owner check runs on every wait pass
# and the pass sleeps 0.1 s between them, so a follower that ends on its dead
# owner has ended about one pass after that owner's death. Two and a half
# seconds is twenty-five passes: long enough that a starved pass cannot explain
# a process that only just outlived the shell, and short enough to stay quick.
SPAN_SECONDS = 2.5

# An ending follower leaves on the pass after its reader closes and then pays
# its own teardown; a few seconds bound the whole of that.
EXIT_WITHIN_SECONDS = 8.5


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


# The reader stands in for the caller's ``grep``: it holds the pipe's read end
# and appends each line it receives, so a row's arrival is read from a file
# rather than inferred from a pipeline. Killing it closes the read end.
_READER = """
import sys

path = sys.argv[1]
with open(path, "a", encoding="utf-8") as handle:
    for line in sys.stdin:
        handle.write(line)
        handle.flush()
"""

# The follower is launched from a process whose argv carries this code, so the
# image's own pid is recorded before the command starts and the case can watch
# it leave the process table. The launcher becomes the follower in place, as
# the real command does.
_LAUNCH = """
import os

directory = os.environ["FOLLOWER_TEST_PID_DIR"]
tag = os.environ["FOLLOWER_TEST_TAG"]
with open(os.path.join(directory, tag + ".pid"), "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
from reckon.cli import main
main()
"""


def _arm_script(sessions: tuple[str, ...]) -> str:
    """The shell that stamps its own pid as the owner and starts both followers.

    The stamp is written before either job starts, and the shell then exits
    without waiting: that exit is the event under test.
    """
    arms = "\n".join(
        f'FOLLOWER_TEST_TAG={session} "$FOLLOWER_TEST_PYTHON" -c'
        f' "$FOLLOWER_TEST_LAUNCH" crew follow --project'
        f' "$FOLLOWER_TEST_PROJECT" --session {session} --no-color &'
        for session in sessions
    )
    return f'export {OWNER_ENV}="{{\\"pid\\": $$, "start_time": ""}}"\n{arms}\n'


def _wait_for_pid(pid_dir: Path, tag: str) -> int:
    """The pid a launcher recorded, waiting for the file to appear."""
    path = pid_dir / f"{tag}.pid"
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            text = ""
        if text.isdigit():
            return int(text)
        time.sleep(POLL_SECONDS)
    pytest.fail(f"{tag}: the follower never recorded its pid")


def _exited(pid: int) -> bool:
    return runs.process_alive(pid) is not True


def _wait_until_exited(pid: int, *, tag: str) -> None:
    deadline = time.monotonic() + EXIT_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if _exited(pid):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"{tag}: the follower was still running {EXIT_WITHIN_SECONDS}s after the "
        "reader closed, so it did not notice that no reader holds the pipe"
    )


@contextlib.contextmanager
def _armed(home: Path, sessions: tuple[str, ...] = (SESSION_A, SESSION_B)):
    """Arm both followers from a shell that exits, and yield what the case reads.

    The reader's stdin is the followers' stdout: the shell is started with that
    pipe as its own stdout, both background jobs inherit it, and the parent's
    copy is closed at once, so the pipe is held by exactly the followers and the
    reader. Yields ``(reader, pids)`` after the shell has exited, so every
    observation the caller makes is made with the arming process gone.
    """
    pid_dir = home / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    rows_path = home / "reader.txt"

    environment = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "RECKON_HOME": str(home),
        "FOLLOWER_TEST_PROJECT": PROJECT,
        "FOLLOWER_TEST_PID_DIR": str(pid_dir),
        "FOLLOWER_TEST_LAUNCH": _LAUNCH,
        "FOLLOWER_TEST_PYTHON": sys.executable,
    }
    environment.pop(OWNER_ENV, None)

    reader = subprocess.Popen(
        [sys.executable, "-c", _READER, str(rows_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shell = subprocess.Popen(
        ["bash", "-c", _arm_script(sessions)],
        stdin=subprocess.DEVNULL,
        stdout=reader.stdin,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=str(REPO_ROOT),
    )
    assert reader.stdin is not None
    reader.stdin.close()
    pids: tuple[int, ...] = ()
    try:
        try:
            shell.wait(timeout=ARM_WITHIN_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail("the arming shell did not exit")
        pids = tuple(_wait_for_pid(pid_dir, session) for session in sessions)
        yield reader, pids
    finally:
        for pid in pids:
            if not _exited(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 9)
        if reader.poll() is None:
            reader.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                reader.communicate(timeout=EXIT_WITHIN_SECONDS)


@contextlib.contextmanager
def _armed_to_device(home: Path, device: Path, session: str = SESSION_A):
    """Arm one follower whose stdout is ``device``, from a shell that exits.

    The shell starts the follower as a background job with its own stdout
    redirected to the device, then exits, so the follower is re-parented with
    a dead owner and a stdout that is not a pipe.
    """
    pid_dir = home / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)

    environment = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "RECKON_HOME": str(home),
        "FOLLOWER_TEST_PROJECT": PROJECT,
        "FOLLOWER_TEST_PID_DIR": str(pid_dir),
        "FOLLOWER_TEST_LAUNCH": _LAUNCH,
        "FOLLOWER_TEST_PYTHON": sys.executable,
    }
    environment.pop(OWNER_ENV, None)

    script = _arm_script((session,)).replace(" &", f" > {device} 2>&1 &")
    shell = subprocess.Popen(
        ["bash", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=str(REPO_ROOT),
    )
    pid = 0
    try:
        try:
            shell.wait(timeout=ARM_WITHIN_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail("the arming shell did not exit")
        pid = _wait_for_pid(pid_dir, session)
        yield pid
    finally:
        if pid and not _exited(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)


def test_a_follower_writing_into_a_device_with_no_reader_ends(home) -> None:
    """A device that discards its writes holds no reader, so the follower ends.

    ``/dev/null`` accepts every write and delivers it nowhere, so a follower
    whose output goes there can never discover a lost reader by a failed
    write. Left as a reader by the descriptor-kind rule it would outlive its
    owner indefinitely and hold its session's registration, refusing every
    dispatch from that session.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    with _armed_to_device(home, Path(os.devnull)) as pid:
        _wait_until_exited(pid, tag=SESSION_A)

    state = runs.follower_state(PROJECT, SESSION_A)
    assert state["registered"] is False, (
        "the registration must be free once a follower writing into a device "
        f"that nobody reads has ended; it still reads {state['follower']!r}"
    )
    assert _tree(real_dir) == before == set()


def test_the_reader_question_answers_by_descriptor_kind(tmp_path) -> None:
    """Each descriptor kind answers the reader question the way it delivers.

    A regular file and a character device deliver to nobody while the command
    runs; a socket carries its peer and a terminal has a screen attached, so
    both reach someone. This is the branch a follower's ending turns on, and
    it is invisible to a case that only ever arms the follower with a pipe.
    """
    with open(tmp_path / "log.txt", "w", encoding="utf-8") as handle:
        assert _stdout_has_reader(1, handle) is False, "a regular file has no reader"
    with open(os.devnull, "w", encoding="utf-8") as handle:
        assert _stdout_has_reader(1, handle) is False, "/dev/null has no reader"

    master, slave = os.openpty()
    try:
        with os.fdopen(master, "wb") as handle:
            assert _stdout_has_reader(1, handle) is True, "a terminal has a reader"
    finally:
        with contextlib.suppress(OSError):
            os.close(slave)

    peer, ours = socket.socketpair()
    try:
        with os.fdopen(ours.fileno(), "wb") as handle:
            assert _stdout_has_reader(1, handle) is True, "a socket has a peer"
    finally:
        peer.close()


def test_two_followers_outlive_the_shell_that_armed_them(home) -> None:
    """Both followers keep streaming into the reader after the shell exits.

    The shell has exited before the case observes anything, so a follower that
    ends on its arming process is already on its way out. Liveness alone is a
    weak measure — a stopped but resident process would satisfy it — so each
    follower must also still be serving its own session: its registration is
    held, and it names the process this case armed. A follower that had ended,
    or one that had given its session up, fails that.

    Rows are deliberately not the instrument. A row appears when the watched
    stream produces one, and this case has no producer; what is under test is
    that the follower keeps its session while a reader holds the pipe. The second
    case supplies the rest: the only event between them is the reader's closure.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    with _armed(home) as (_reader, pids):
        for pid in pids:
            assert not _exited(pid), (
                "a follower ended within the arm because the shell that started "
                "it had exited, so it judged the owner and not the reader"
            )
        time.sleep(SPAN_SECONDS)
        for session, pid in zip((SESSION_A, SESSION_B), pids, strict=True):
            assert not _exited(pid), (
                "a follower ended inside the span while the reader still held "
                "the pipe, so it judged the owner and not the reader"
            )
            state = runs.follower_state(PROJECT, session)
            assert state["registered"] is True, (
                f"the {session} follower gave its session up inside the span "
                "although the reader still held the pipe, so it ended for its "
                f"arming process rather than for its reader ({state['follower']!r})"
            )
            assert str(state["follower"].get("pid")) == str(pid), (
                f"the {session} registration must name the follower this case "
                f"armed, {pid}; it reads {state['follower'].get('pid')!r} while "
                "that process is still live"
            )

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )


def test_closing_the_reader_ends_both_followers_and_frees_them(home) -> None:
    """Closing the reader ends both followers, and both registrations are free.

    The reader is killed, which closes the pipe's read end: the moment a write
    would fail. The owner check must end both followers there rather than
    leaving them holding their registrations against the session's next
    follower, which would refuse every dispatch from the session.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    with _armed(home) as (reader, pids):
        assert reader.poll() is None, (
            "the reader must be live before it is closed, or the case would "
            "measure a pipe that was never open"
        )
        reader.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            reader.communicate(timeout=EXIT_WITHIN_SECONDS)

        for pid, session in zip(pids, (SESSION_A, SESSION_B), strict=True):
            _wait_until_exited(pid, tag=session)

    for session in (SESSION_A, SESSION_B):
        state = runs.follower_state(PROJECT, session)
        assert state["registered"] is False, (
            f"the {session} registration must be free once its follower ended; "
            f"it still reads {state['follower']!r}"
        )

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )
