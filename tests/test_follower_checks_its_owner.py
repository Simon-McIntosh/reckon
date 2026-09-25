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

Carrying the owner across a reload needs its own case, because the obvious one
does not test it. Reloading while the owner is alive leaves the parent unchanged
— ``execve`` does not re-parent — so a replacement that read ``os.getppid()``
would read the owner either way and the case would pass with the carry removed.
The owner's *identity* must therefore be read while the parent is something
other than the owner: the arming stub dies, the follower is adopted by a live
child subreaper, and only then is it reloaded. Resolved from the carried owner
the replacement leaves; resolved from the parent it would run on forever.

The gate declares one negative control, executed and logged: restoring the early
return in ``_check_consumer`` for a non-holder, and making ``acquire()`` record
``os.getppid()`` again, must fail this file.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
# The exit a dead owner triggers is measured on this login node at 0.811, 0.839
# and 0.905 s for a read-only follower and 0.185, 0.242 and 0.185 s for the
# holder, from its owner's death to the process leaving. Two seconds is 2.2x the
# slowest sample, which leaves room for the interpreter teardown and the jitter
# of a loaded node while staying short enough to be a bound rather than a wait.
# The follower's wait pass polls every 0.1 s (``poll_interval`` in
# ``_follow_watch_lines``), so five seconds would allow fifty passes: a
# regression that asked about the owner once every fifty passes would still
# pass it. Two seconds allows about twenty, and the samples show the check
# answering on the pass it runs. The live-owner control shows the same window is
# long enough to mean something: a follower which never checked would still be
# running at its end.
EXIT_WITHIN_SECONDS = 2.0
POLL_SECONDS = 0.05
RELOAD_WITHIN_SECONDS = 8.0
# The reloader polls its source stamp at most once per
# ``runs.FOLLOWER_FRESHNESS_SECONDS``. A stopped follower must be held still for
# longer than that before it is resumed, so the first pass after it resumes can
# see the stamp change rather than being rate-limited past it.
FRESHNESS_SECONDS = 1.0
SETTLE_SECONDS = FRESHNESS_SECONDS + 0.4
SIGSTOP = 19
SIGCONT = 18
# The replacement image is watched tightly: its command line is what shows the
# reload, and it lives only as long as its own imports take, so a sample per
# wait-pass would risk missing it entirely.
SAMPLE_SECONDS = 0.005
# From the replacement image becoming live to the process leaving. That window
# carries the replacement's interpreter and package import cost, which the
# owner-exit bound above is not about, so it is measured and bounded on its own:
# 1.193, 1.220 and 1.240 s over three runs, and three seconds is 2.4x the
# slowest. The failing direction this bound has to catch is unbounded — without
# the carried owner the replacement reads a live parent and never leaves — so
# the bound separates a follower that left from one that never will.
RELOAD_EXIT_WITHIN_SECONDS = 3.0

# The replacement image is launched with a launcher that inserts its import root
# on ``sys.path`` before entering the command. That string is absent from the
# argv the test arms, so finding it on the process is direct evidence that the
# image was replaced.
RELOAD_LAUNCHER_MARKER = "sys.path.insert"

# What the reload trigger appends to the source. A comment, and one whose every
# prefix is also a comment, so the module parses with the change in place and a
# reader that imports it mid-window is unaffected.
_SOURCE_CHANGE_BYTES = b"\n# follower reload probe\n"

# The cases that force a source change edit one file on disk, and each restores
# what it read before editing. Two of them running at once can therefore capture
# and write back each other's edit, leaving the append behind in the tree and
# comparing against a baseline neither of them set. One worker owns the group, so
# the cases that touch the source run one at a time.
SOURCE_CHANGE_GROUP = "follower-source-change"

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

# A live stand-in for the process that replaces a dead owner. It marks itself a
# child subreaper, then starts the same stub the other cases use; when that stub
# is killed its child is handed to this process rather than to init, and this
# process stays alive so the adopted child hangs off something live.
_SUBREAPER = """
import ctypes, os, subprocess, sys, time

stub_pid_path, stub_source, *stub_args = sys.argv[1:]
libc = ctypes.CDLL("libc.so.6", use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise SystemExit("prctl(PR_SET_CHILD_SUBREAPER): " + os.strerror(ctypes.get_errno()))
proc = subprocess.Popen([sys.executable, "-c", stub_source, *stub_args])
with open(stub_pid_path, "w", encoding="utf-8") as handle:
    handle.write(str(proc.pid))
proc.wait()
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


def _start_subreaper_stub(
    home: Path, workdir: Path, tag: str
) -> tuple[subprocess.Popen, Path, Path]:
    """Start an owner stub whose own parent outlives it.

    Returns the subreaper process, the path its stub's pid lands in, and the
    path the stub's follower's pid lands in. Killing the stub hands the follower
    to the subreaper rather than to init, which is the state a follower is left
    in when its arming session dies under a live process that reaps orphans.
    """
    stub_pid_path = workdir / f"{tag}.stub.pid"
    pid_path = workdir / f"{tag}.pid"
    environment = _stub_env(home)
    environment["FOLLOWER_TEST_PROJECT"] = PROJECT
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SUBREAPER,
            str(stub_pid_path),
            _STUB,
            str(REPO_ROOT),
            str(workdir / f"{tag}.stdout"),
            str(workdir / f"{tag}.stderr"),
            str(pid_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    return process, stub_pid_path, pid_path


def _process_ppid(pid: int) -> str:
    """The parent pid from the per-process stat record, or an empty string."""
    fields = runs._process_stat_fields(pid)
    return fields[1] if len(fields) > 1 else ""


def _wait_until_stopped(pid: int, *, tag: str) -> None:
    """Wait until the follower is held still by a stop signal.

    Freezing the follower is what puts the re-parenting before the reload: a
    process that is stopped cannot run its wait pass, so the owner's death and
    the adoption happen while nothing has yet looked at the owner.
    """
    deadline = time.monotonic() + EXIT_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if runs._process_state(pid) == "T":
            return
        if _exited(pid):
            pytest.fail(f"{tag}: the follower ended before it was held still")
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"{tag}: the follower was never held still, so the re-parenting "
        "could not be ordered ahead of the reload"
    )


def _wait_until_gone(pid: int, *, tag: str) -> None:
    """Wait for a process that is not this test's child to leave the table."""
    deadline = time.monotonic() + EXIT_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if runs.process_alive(pid) is not True:
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(f"{tag}: the process was still alive after it was killed")


def _wait_until_reloaded_then_exited(pid: int, *, tag: str) -> tuple[bool, float]:
    """Wait for the replacement image, then for the process to leave.

    The reload and the exit are two states of one pid, so each is waited for by
    the helper that owns it. The clock for the exit starts when the launcher
    appears, so the replacement's own import cost is not charged against the
    owner-exit bound. Returns whether the replacement was seen and how long it
    took to leave after that.
    """
    _observe_reload(pid, tag=tag)
    return True, _wait_until_left_after_reload(pid, tag=tag)


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


def _read_pid(pid_path: Path, holder: subprocess.Popen, tag: str) -> int:
    """Read a pid another process writes, while its holder is still alive."""
    deadline = time.monotonic() + ARM_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if holder.poll() is not None:
            _stdout, stderr = holder.communicate(timeout=EXIT_WITHIN_SECONDS)
            pytest.fail(
                f"{tag}: the holder exited before it recorded a pid; stderr={stderr!r}"
            )
        if pid_path.is_file():
            text = pid_path.read_text().strip()
            if text.isdigit():
                return int(text)
        time.sleep(POLL_SECONDS)
    pytest.fail(f"{tag}: no pid was ever recorded")


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


def _observe_reload(pid: int, *, tag: str) -> None:
    """Wait for the replacement image, identified by its own launcher.

    The replacement's command line carries the launcher that inserts the import
    root, which the armed argv does not. An image replacement is the only thing
    that can put it there, so this is the reload rather than a proxy. A follower
    that leaves before the launcher appears fails here rather than passing by
    measuring the first image alone. Its command line is sampled tightly rather
    than once per wait-pass, because the replacement is identified by that line
    and lives only as long as its own imports take.
    """
    deadline = time.monotonic() + RELOAD_WITHIN_SECONDS
    while time.monotonic() < deadline:
        if RELOAD_LAUNCHER_MARKER in _process_argv(pid):
            return
        if _exited(pid):
            pytest.fail(
                f"{tag}: the follower left without replacing its image, so the "
                "owner's survival across a reload was not exercised; "
                f"argv={_process_argv(pid)!r}"
            )
        time.sleep(SAMPLE_SECONDS)
    pytest.fail(
        f"{tag}: the follower did not replace its image; argv={_process_argv(pid)!r}"
    )


def _wait_until_left_after_reload(pid: int, *, tag: str) -> float:
    """Wait for the replacement image to leave; return how long it took.

    The window carries the replacement's own interpreter and package import
    cost, which the owner-exit bound is not about, so it is measured and bounded
    on its own. Never killed to make it so: the property under measure is that a
    dead owner ends the replacement, and a killed process would answer a
    different question. The clock starts once the caller has observed the
    owner's death.
    """
    started = time.monotonic()
    while time.monotonic() - started < RELOAD_EXIT_WITHIN_SECONDS:
        if _exited(pid):
            return time.monotonic() - started
        time.sleep(SAMPLE_SECONDS)
    pytest.fail(
        f"{tag}: the replacement image was still running "
        f"{RELOAD_EXIT_WITHIN_SECONDS!r}s after its owner was killed, so the "
        "owner it resolved is alive: the arming's owner was not carried across "
        f"the reload; argv={_process_argv(pid)!r}"
    )


def _wait_until_looping(home: Path, pid: int, *, tag: str) -> None:
    """Wait until the follower's stream loop has entered its first wait pass.

    The registration record is written as the follower enters its stream loop,
    and the reloader fixes its source baseline partway through that entry, so a
    source change made the moment the record appears can be captured as the
    baseline itself and never seen as a change: the follower then runs on with a
    stale image and the case measures nothing. The loop's first pass records a
    recovery status naming the sweeping pid, and no earlier pass can write it, so
    that record is the barrier — once it names this follower, the baseline was
    fixed before this function returned and a change made after it is a change.
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
        if _exited(pid):
            pytest.fail(f"{tag}: the follower ended before it entered its wait loop")
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"{tag}: the follower never recorded a wait pass, so its source baseline "
        "was not known to be fixed before this case changed the source"
    )


@contextlib.contextmanager
def _source_mutation_window():
    """Hold the one window in which a case may edit the source the follower stamps.

    The stamp is computed over the imported package, so every case that forces a
    reload edits a path all pytest workers share. Two of them editing at once can
    read each other's append as their own baseline and write it back after their
    restore, which leaves the change in the tree and has the follower compared
    against a stamp neither case set. The distributor does not keep these cases
    apart on its own — spreading by load is its default — so the window is held
    under an exclusive lock instead.
    """
    lock_path = Path(tempfile.gettempdir()) / "reckon-follower-source-change.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _force_source_change() -> tuple[bytes, int, int]:
    """Advance the follower's source stamp with a real content change.

    The stamp covers each source file's size and its mtime together, and an edit
    advances both, which is the change the follower exists to notice. The appended
    line is a comment, and every prefix of it, so the module parses while the
    change is in place. A change made before the follower has fixed its baseline
    is captured as the baseline and never seen at all, so the caller waits for
    ``_wait_until_looping`` first. What to restore is handed back with it.
    """
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


@pytest.mark.xdist_group(SOURCE_CHANGE_GROUP)
def test_a_reloaded_follower_still_leaves_when_the_original_owner_dies(home) -> None:
    """A follower that replaced its image still exits after the owner dies.

    The owner is fixed at the first arming and carried to the replacement image
    through the environment ``os.execve`` passes, so a reload continues the
    original arming rather than adopting whatever process replaced the image.
    The reload is forced through the same trigger the follower uses for a source
    change, and its occurrence is confirmed by the replacement's own command
    line, so a run in which no reload happened cannot pass by measuring the
    first image alone.

    This case reloads while the owner is alive, so it cannot see the carry:
    ``execve`` does not change the parent, so a replacement reading
    ``os.getppid()`` reads the owner here and leaves under the owner's later
    death either way. ``test_a_reload_under_a_live_subreaper_keeps_the_dead_owner``
    is the case that reads the owner where the parent is not the owner, and it is
    the one that fails if the carry is removed.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    owner: subprocess.Popen | None = None
    follower_pid: int | None = None
    restore: tuple[bytes, int, int] | None = None
    with _source_mutation_window():
        try:
            owner, pid_path = _start_stub(home, home, "reload")
            follower_pid = _follower_pid(pid_path, owner, "reload")
            _wait_until_holder(
                follower_pid,
                deadline=time.monotonic() + ARM_WITHIN_SECONDS,
                tag="reload",
            )

            _wait_until_looping(home, follower_pid, tag="reload")

            restore = _force_source_change()
            _observe_reload(follower_pid, tag="reload")

            _kill_and_reap(owner)
            elapsed = _wait_until_left_after_reload(
                follower_pid, tag="reloaded follower"
            )
        finally:
            if follower_pid is not None and not _exited(follower_pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(follower_pid, 9)
            _kill(owner)
            if restore is not None:
                _restore_source_bytes(restore)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )
    assert elapsed <= RELOAD_EXIT_WITHIN_SECONDS


@pytest.mark.xdist_group(SOURCE_CHANGE_GROUP)
def test_a_reload_under_a_live_subreaper_keeps_the_dead_owner(home) -> None:
    """A follower re-parented to a live process still leaves on its dead owner.

    This is the case that sees the carried owner. The arming stub is killed, so
    the follower is handed to a live child subreaper — the process a follower is
    left hanging from whenever its session dies under something that reaps
    orphans. The follower is held still across that, so nothing has looked at
    the owner yet, and then its source stamp is advanced and it is resumed.

    On its first pass after resuming it replaces its image, and the replacement
    resolves the process it reports to. Resolved from ``os.getppid()`` that is
    the live subreaper, and the replacement would then run on with an owner that
    never died — which is why the original reload case cannot see this: there
    the reload happens while the owner is alive, so the parent it would read is
    the owner either way. Resolved from the carried ``RECKON_FOLLOWER_OWNER`` it
    is the dead stub, and the replacement leaves.

    The adoption is asserted, not assumed: the follower's parent must be the
    subreaper before it is resumed, so a run in which the kill left it under
    init cannot pass by measuring the wrong tree.
    """
    real_dir = _real_follower_dir(PROJECT)
    before = _tree(real_dir)

    subreaper: subprocess.Popen | None = None
    follower_pid: int | None = None
    stub_pid: int | None = None
    restore: tuple[bytes, int, int] | None = None
    with _source_mutation_window():
        try:
            subreaper, stub_pid_path, pid_path = _start_subreaper_stub(
                home, home, "orphan"
            )
            follower_pid = _follower_pid(pid_path, subreaper, "orphan")
            _wait_until_holder(
                follower_pid,
                deadline=time.monotonic() + ARM_WITHIN_SECONDS,
                tag="orphan",
            )
            _wait_until_looping(home, follower_pid, tag="orphan")

            os.kill(follower_pid, SIGSTOP)
            _wait_until_stopped(follower_pid, tag="orphan")

            stub_pid = _read_pid(stub_pid_path, subreaper, "orphan")
            os.kill(stub_pid, 9)
            _wait_until_gone(stub_pid, tag="arming stub")

            adopted_by = _process_ppid(follower_pid)
            assert adopted_by == str(subreaper.pid), (
                "the follower must be adopted by the live subreaper before it is "
                f"resumed; its parent is {adopted_by!r}, not {subreaper.pid}"
            )

            restore = _force_source_change()
            time.sleep(SETTLE_SECONDS)

            os.kill(follower_pid, SIGCONT)
            saw_reload, elapsed = _wait_until_reloaded_then_exited(
                follower_pid, tag="re-parented follower"
            )
        finally:
            if follower_pid is not None and not _exited(follower_pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(follower_pid, 9)
            if stub_pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(stub_pid, 9)
            _kill(subreaper)
            if restore is not None:
                _restore_source_bytes(restore)

    assert _tree(real_dir) == before == set(), (
        "a follower pointed at a temporary home must leave the real follower "
        "directory untouched"
    )
    assert saw_reload, "the case must observe the reload it depends on"
    assert elapsed <= RELOAD_EXIT_WITHIN_SECONDS


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
