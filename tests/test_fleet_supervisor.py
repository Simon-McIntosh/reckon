"""The fleet batch step reads requests and starts what each one names.

The reader is exercised over a real FIFO in a temporary runtime directory,
against the module's own ``python -m`` entry point, because the property under
measure is what a request line does to the machine: a spawned child exists, a
session start reaches zellij, the inherited session variables do not reach the
child. Each case therefore observes a process or a file rather than a return
value, and every absence is preceded by showing the same instrument reading a
known-present value.

The configuration home the reader resolves its declared services through is
pinned inside each case's temporary directory, so no case here reads the
operator's fleet config or starts a service it declares. The reader's own stop
request is what every case stops a reader with, so the services it started end
with it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import re
import resource
import signal
import stat
import struct
import subprocess
import sys
import termios
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import fleet_supervisor
from reckon.crew.fleet_supervisor import Request

REPO_ROOT = str(Path(fleet_supervisor.__file__).resolve().parents[2])
MODULE_ARGV = [sys.executable, "-m", "reckon.crew.fleet_supervisor"]

WAIT_SECONDS = 20.0
POLL_SECONDS = 0.05

# How long a reader is given to collect an exited child while no request is
# arriving. The reader's own collection interval is far shorter, so a reader
# that collects on its interval answers well inside this bound, and a reader
# that collects only between requests never answers at all.
COLLECT_SECONDS = 5.0

# A child that proves it ran and then stays alive, so the pid the reader
# records can be read while the process it names is still there.
LIVE_CHILD = (
    "import sys, time\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write('ran\\n')\n"
    "time.sleep(60)\n"
)

# A child that proves it ran and then exits on its own, so the reader holds an
# exited child it must collect without a request arriving.
EXITING_CHILD = "import sys\nopen(sys.argv[1], 'w', encoding='utf-8').write('ran\\n')\n"

# A child that appends a line to a marker each time it runs, so a request acted
# on more than once shows as more than one line in the marker.
APPENDING_CHILD = (
    "import sys\nopen(sys.argv[1], 'a', encoding='utf-8').write('ran\\n')\n"
)

# A child that records the environment it was handed and exits.
ENV_DUMP_CHILD = (
    "import os, sys\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
    "    handle.write('\\n'.join(sorted(os.environ)))\n"
)

# A declared service stub: argv[1] is a marker it writes to prove it ran, and
# then it idles in a sleep loop. A loop rather than one long sleep, because the
# case stops it and what the stop reaches must be a process that would still be
# running otherwise.
STUB_SERVICE = (
    "import sys, time\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write('started\\n')\n"
    "while True:\n"
    "    time.sleep(0.1)\n"
)

# How long a reader is given to answer a stop request before it is killed. The
# bound covers the reader's own service stop grace, so a reader that is honoring
# the request is never killed mid-stop.
GRACEFUL_STOP_SECONDS = 30.0

# A stand-in for zellij: it appends its argv to the file named by
# RECKON_ZELLIJ_STUB_LOG, prints the sessions named by
# RECKON_ZELLIJ_STUB_SESSIONS when asked to list them, and prints the tabs
# named by RECKON_ZELLIJ_STUB_TAB_NAMES when asked for the tab names. The names
# deliberately avoid the ZELLIJ prefix, which the reader strips from every
# environment it passes on, so the stub would lose its own configuration.
# Nothing is left to the real zellij for the reader to start a session on this
# machine.
#
# An ``attach`` blocks, so a client that is only asked to leave is still there
# afterwards: the reader's detach is observable as the process ending, which it
# would not be if the stub exited on its own. The sleep is bounded, so nothing
# survives a run that does not detach for more than half a minute.
ZELLIJ_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$RECKON_ZELLIJ_STUB_LOG"
case "$1" in
  list-sessions) printf '%s' "$RECKON_ZELLIJ_STUB_SESSIONS" ;;
  action) printf '%s' "$RECKON_ZELLIJ_STUB_TAB_NAMES" ;;
  attach) exec sleep 30 ;;
esac
exit 0
"""


def _wait_for(predicate, *, message: str, timeout: float = WAIT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_SECONDS)
    pytest.fail(message)


def _kill_pid(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _send(runtime: Path, line: str) -> None:
    with open(runtime / "requests", "w", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _send_chunks(runtime: Path, chunks: list[str]) -> None:
    """Write one request line to the FIFO in several writes.

    A read on a FIFO returns what is available rather than waiting for a whole
    line, so a request written in pieces can reach the reader split across two
    reads. Each piece is written on the reader's own open descriptor and given a
    pause, so the reader's bounded wait wakes and reads between them rather than
    the pieces coalescing in the kernel buffer.
    """
    descriptor = os.open(
        runtime / fleet_supervisor.REQUEST_FIFO_NAME,
        os.O_WRONLY | os.O_NONBLOCK,
    )
    try:
        for chunk in chunks:
            os.write(descriptor, chunk.encode())
            time.sleep(0.3)
    finally:
        os.close(descriptor)


def _try_send(runtime: Path, line: str) -> bool:
    """Write a request line if a reader holds the FIFO's other end.

    Opening a FIFO for writing blocks until a reader opens it, so a reader that
    has already ended would hang the writer forever; the open is non-blocking
    and answers ENXIO instead, which is the fact the caller wants rather than a
    wait. The line is short, so the write itself cannot block.
    """
    try:
        descriptor = os.open(
            runtime / fleet_supervisor.REQUEST_FIFO_NAME,
            os.O_WRONLY | os.O_NONBLOCK,
        )
    except OSError:
        return False
    try:
        os.write(descriptor, (line + "\n").encode())
    finally:
        os.close(descriptor)
    return True


def _processes_with_env(name: str, value: str) -> list[int]:
    """Every process of this user whose environment carries ``name=value``.

    Each process is read from its own environment file under ``/proc``, so it is
    found by what it was handed rather than by what it is called: a name pattern
    would match the search itself, and would miss a service whose argv is
    whatever the config declared. A process owned by another user and one that
    ends while the scan runs are skipped rather than reported, so a missing
    reading is never mistaken for an absent process.
    """
    needle = f"{name}={value}".encode()
    uid = os.getuid()
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.append(int(entry.name))
    return sorted(found)


def _reader_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _argv_log(path: Path) -> str:
    """The zellij stub's recorded invocations, or empty before it has run."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _settled_argv(
    path: Path, *, quiet: float = 0.5, timeout: float = WAIT_SECONDS
) -> str:
    """The stub's invocations once no new one has been recorded for ``quiet``.

    An ordering read out of a live log is only meaningful when nothing further
    is coming: the session line runs several zellij commands in sequence, so a
    read taken between two of them would report the later one missing. The wait
    is bounded, and returns whatever was last seen, so a caller always gets a
    string to assert against rather than a hanging read.
    """
    deadline = time.monotonic() + timeout
    previous: str | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        current = _argv_log(path)
        if current and current == previous:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= quiet:
                return current
        else:
            stable_since = None
        previous = current
        time.sleep(POLL_SECONDS)
    return _argv_log(path)


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path, monkeypatch):
    """Resolve the fleet services file inside this case's temporary directory.

    The reader starts every service ``<config home>/fleet/services.json``
    declares, each as a process that leads its own session and so outlives
    nothing but the allocation. Without this the file's own runs resolve the
    operator's real config and launch real services that outlive the case, which
    is state outside the repository in the writing direction. ``XDG_CONFIG_HOME``
    is the environment the reader resolves the path through, so this one
    variable drives the whole configuration into the case's tree.
    """
    config_home = tmp_path / "config-home"
    monkeypatch.setenv(fleet_supervisor.CONFIG_HOME_ENV, str(config_home))
    return config_home


@pytest.fixture()
def reader(tmp_path):
    """A live reader over a FIFO in a temporary runtime directory."""
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    log = tmp_path / "reader.log"
    opened: list[tuple[subprocess.Popen, object]] = []
    stack = ExitStack()

    def start(extra_env: dict[str, str] | None = None) -> subprocess.Popen:
        env = {
            **os.environ,
            "FLEET_RUNTIME_DIR": str(runtime),
            "FLEET_STATE_DIR": str(state),
            # The machine's own sampler would outlive the case and sample into
            # the case's state; the one case about the sampler names a stub.
            "FLEET_HEALTH_SAMPLER": "",
            "PYTHONPATH": REPO_ROOT,
        }
        if extra_env:
            env.update(extra_env)
        handle = open(log, "ab")  # noqa: SIM115 - closed by the stack at teardown
        stack.callback(handle.close)
        process = subprocess.Popen(
            MODULE_ARGV,
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
        )
        opened.append((process, handle))
        _wait_for(
            (runtime / "requests").exists,
            message=(
                "the reader never created its request FIFO, so no request could "
                f"be delivered to it; log={_reader_log(log)!r}"
            ),
        )
        return process

    def stop(timeout: float = GRACEFUL_STOP_SECONDS) -> None:
        """Stop every reader this fixture started, and what each one started.

        The stop request is the reader's own, so the services it declared are
        stopped before it exits and nothing it started is left behind. A reader
        that does not answer within the bound is killed, which is the only case
        where what it started could survive it.
        """
        for process, _handle in opened:
            if process.poll() is not None:
                continue
            _try_send(runtime, "stop")
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    yield SimpleNamespace(
        start=start, stop=stop, runtime=runtime, state=state, log=log, opened=opened
    )
    stop()
    stack.close()


def _write_spec(run_directory: Path, argv: list[str]) -> Path:
    run_directory.mkdir(parents=True, exist_ok=True)
    spec = run_directory / "supervisor.json"
    spec.write_text(
        json.dumps(
            {
                "run_id": "r-test",
                "run_directory": str(run_directory),
                "cwd": str(run_directory),
                "argv": argv,
            }
        ),
        encoding="utf-8",
    )
    return spec


def _wait_for_spawned(run_directory: Path) -> dict:
    """The acknowledgement the spawn verb writes, or a failure naming it."""
    spawned = run_directory / "spawned.json"
    return _wait_for(
        lambda: json.loads(spawned.read_text()) if spawned.exists() else None,
        message=(
            f"the spawn verb wrote no {spawned}, so the pid it acknowledged "
            "could not be read; no pid assertion is satisfiable without it"
        ),
    )


def _fleet_state_snapshot(directory: Path) -> dict:
    """Everything a write into a state directory would move.

    The machine's own fleet state belongs to a live batch step: the suite reads
    it and never writes it, so this reading is taken on both sides of a case and
    compared. Absence is a value here — a directory that does not exist and one
    holding a record are different readings, so a writer cannot make the two
    ends of a comparison agree by deleting what it wrote.
    """
    if not directory.exists():
        return {"exists": False}
    entries = sorted(directory.iterdir())
    record = directory / fleet_supervisor.RECORD_NAME
    return {
        "exists": True,
        "listing": [entry.name for entry in entries],
        "mtimes": {entry.name: entry.stat().st_mtime_ns for entry in entries},
        "record_sha256": (
            hashlib.sha256(record.read_bytes()).hexdigest() if record.exists() else None
        ),
    }


def test_a_spawn_line_runs_the_stub_and_records_its_live_pid(reader, tmp_path) -> None:
    """A spawn line runs the spec's argv and acknowledges a live pid.

    The stub writes a marker before it sleeps, so the marker shows the argv ran
    and the still-live pid shows the recorded number names that process rather
    than a number assigned to one that has already exited.
    """
    run_directory = tmp_path / "run"
    marker = tmp_path / "stub-ran"
    real_state = fleet_supervisor.state_directory({})
    state_before = _fleet_state_snapshot(real_state)
    stub = tmp_path / "stub.py"
    stub.write_text(LIVE_CHILD, encoding="utf-8")
    spec = _write_spec(run_directory, [sys.executable, str(stub), str(marker)])

    reader.start()
    child_pid = None
    try:
        _send(reader.runtime, f"spawn r-test {spec}")
        spawned = _wait_for_spawned(run_directory)
        child_pid = int(spawned["pid"])
        _wait_for(
            marker.exists,
            message="the spawned argv never ran, so the stub it names did not start",
        )
        os.kill(child_pid, 0)
    finally:
        if child_pid is not None:
            _kill_pid(child_pid)

    assert spawned["started_at"].endswith("Z"), spawned
    assert len(spawned["started_at"]) >= 20, spawned
    # The record's directory was named by the spec, not guessed.
    assert (run_directory / "spawned.json").exists()
    # The reader kept running and left its own record of where it lives.
    record = json.loads((reader.state / "record.json").read_text())
    assert record["runtime_dir"] == str(reader.runtime)
    assert record["node"], record

    # The machine's own fleet state is a live batch step's, so the suite reads
    # it and never writes it: the state override is what keeps this run from
    # publishing a fake fleet into the directory every reader resolves. Byte
    # identical — listing, mtimes and the record's digest, not merely "no new
    # files" — so a rewrite of the existing record is caught too.
    assert _fleet_state_snapshot(real_state) == state_before, (
        f"the machine's fleet state at {real_state} changed while the spawn "
        "case ran, so the case wrote there instead of into its own state"
    )
    # The collector is shown to read a record where one exists. The same call
    # against the temporary state directory the reader just wrote must report a
    # digest, or an unchanged real directory would read the same as an unread
    # one and the equality above would say nothing.
    written = _fleet_state_snapshot(reader.state)
    assert written["record_sha256"] is not None, written


def test_an_exited_child_is_collected_with_no_request_arriving(
    reader, tmp_path
) -> None:
    """An exited child is collected on the reader's own interval, not on a request.

    The reader spends its life reading the request FIFO, and requests are rare,
    so a child that exits while the FIFO is idle would be collected only when
    the next request happened to arrive -- leaving an exited supervisor defunct
    for as long as the reader waits. The child here ends on its own and no
    request follows it, so the pid leaving the process table is the reader
    collecting it rather than a request prompting the sweep. The marker proves
    the child ran, and /proc is shown reading the reader's own live pid before
    the child's absence is read from it, so the disappearance is a collected
    child and not one that never started.
    """
    run_directory = tmp_path / "run"
    marker = tmp_path / "child-ran"
    stub = tmp_path / "stub.py"
    stub.write_text(EXITING_CHILD, encoding="utf-8")
    spec = _write_spec(run_directory, [sys.executable, str(stub), str(marker)])

    reader.start()
    reader_pid = reader.opened[0][0].pid
    _send(reader.runtime, f"spawn r-test {spec}")
    spawned = _wait_for_spawned(run_directory)
    child_pid = int(spawned["pid"])
    _wait_for(
        marker.exists,
        message=(
            "the spawned child never recorded that it ran, so there is no exit "
            "to collect and its pid is invisible for a reason other than "
            f"collection; log={_reader_log(reader.log)!r}"
        ),
    )
    # The instrument is shown reading a present pid before an absence is read
    # from it: the reader itself is under /proc now, so an empty result for the
    # child is a collection rather than an instrument that cannot see anything.
    assert Path(f"/proc/{reader_pid}").exists(), (
        f"the reader's own pid {reader_pid} is not visible under /proc, so the "
        "child's absence would say nothing about collection"
    )

    # No further request is sent: the reader must collect the child on its own
    # interval. Under a sweep that runs only between requests, the reader sits
    # in its blocking read and the exited child's pid stays in /proc.
    child = Path(f"/proc/{child_pid}")
    started = time.monotonic()
    _wait_for(
        lambda: not child.exists(),
        message=(
            f"the exited child {child_pid} was still visible under /proc after "
            f"{time.monotonic() - started:.1f}s with no request arriving, so the "
            "reader did not collect it on its own interval"
        ),
        timeout=COLLECT_SECONDS,
    )
    assert time.monotonic() - started < COLLECT_SECONDS


def test_a_request_split_across_reads_is_handled_once_and_whole(
    reader, tmp_path
) -> None:
    """A request written in pieces is reassembled and acted on exactly once.

    A read on the FIFO returns what is available rather than a whole line, so a
    request can arrive split across two of the reader's bounded reads. The
    reader holds a pending buffer and acts only on complete lines, so the two
    halves are joined into one line and handled once. The stub appends a line to
    its marker each time it runs, so a reader that acted on the halves
    separately would either start no child (each half is malformed) or two (the
    marker holding more than one line); one whole request shows as one line.
    """
    run_directory = tmp_path / "run"
    marker = tmp_path / "child-ran"
    stub = tmp_path / "stub.py"
    stub.write_text(APPENDING_CHILD, encoding="utf-8")
    spec = _write_spec(run_directory, [sys.executable, str(stub), str(marker)])

    reader.start()
    line = f"spawn r-test {spec}"
    # Split the line in the middle of the spec path, so neither half is a
    # request on its own and only the joined line names the spec.
    cut = len(line) // 2
    _send_chunks(reader.runtime, [line[:cut], line[cut:] + "\n"])
    _wait_for(
        marker.exists,
        message=(
            "the split request was never acted on, so its halves were not "
            f"reassembled into one line; log={_reader_log(reader.log)!r}"
        ),
    )
    # Give a second handling, were there one, time to land before counting.
    time.sleep(1.0)
    assert marker.read_text(encoding="utf-8").split() == ["ran"], (
        "the request was handled more than once, so its two halves were acted "
        f"on separately: {marker.read_text(encoding='utf-8')!r}"
    )


def test_the_readers_runtime_directory_is_not_world_readable(reader) -> None:
    """The private runtime directory is created mode 0700, not left to umask.

    It holds zellij's sockets and the agent harness's cross-session sockets
    both, so a directory the submitting shell's umask left at 0755 would expose
    every one of them to anyone who can walk to the node's /tmp. The reader
    creates the directory before it creates the FIFO, so the FIFO's arrival
    shows the directory exists and its mode can be read.
    """
    reader.start()
    mode = stat.S_IMODE(os.stat(reader.runtime).st_mode)
    assert mode == 0o700, (
        f"the runtime directory was created mode {oct(mode)}, so the sockets it "
        "holds are reachable by anyone who can walk to it"
    )


def test_a_spawn_line_missing_its_spec_path_is_logged_and_ignored(
    reader, tmp_path
) -> None:
    """A malformed line is logged and the reader carries on.

    The valid line after the malformed ones must still be acted on, so
    "ignored" means the reader survived it rather than merely that something
    was printed while the loop quietly stopped.
    """
    run_directory = tmp_path / "run"
    marker = tmp_path / "stub-ran"
    stub = tmp_path / "stub.py"
    stub.write_text(LIVE_CHILD, encoding="utf-8")
    spec = _write_spec(run_directory, [sys.executable, str(stub), str(marker)])

    reader.start()
    _send(reader.runtime, "spawn r-only-a-run-id")
    _wait_for(
        lambda: "unknown request: spawn r-only-a-run-id" in _reader_log(reader.log),
        message=(
            "a spawn line without its spec path was not logged as an unknown "
            f"request; log={_reader_log(reader.log)!r}"
        ),
    )
    _send(reader.runtime, "not-a-verb at all")
    _wait_for(
        lambda: "unknown request: not-a-verb at all" in _reader_log(reader.log),
        message=(
            "a line naming no known verb was not logged as an unknown request; "
            f"log={_reader_log(reader.log)!r}"
        ),
    )
    _send(reader.runtime, f"spawn r-test {spec}")
    child_pid = None
    try:
        child_pid = int(_wait_for_spawned(run_directory)["pid"])
    finally:
        if child_pid is not None:
            _kill_pid(child_pid)
    assert reader.opened[0][0].poll() is None, "the reader died on a malformed line"


def test_session_starts_zellij_and_reload_reexecutes_the_module(
    reader, tmp_path
) -> None:
    """A session line starts a zellij server; a reload line replaces the image.

    The zellij stub is what makes the session case observable without touching
    this machine's sessions, and the reload case is driven through the handler
    with a recorded exec so it cannot replace the test process.
    """
    stub_log = tmp_path / "zellij-argv.log"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "zellij"
    stub.write_text(ZELLIJ_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "RECKON_ZELLIJ_STUB_LOG": str(stub_log),
        "RECKON_ZELLIJ_STUB_SESSIONS": "",
        "RECKON_ZELLIJ_STUB_TAB_NAMES": "main\n",
    }
    reader.start(env)
    _send(reader.runtime, "session demo")
    # Wait on the invocation rather than on the reader's "starting" line: the
    # line is written before zellij is invoked, so a reader that waited on it
    # could read the log before the create had run.
    _wait_for(
        lambda: (
            "attach --create-background --force-run-commands --create demo"
            in _argv_log(stub_log)
        ),
        message=(
            "the session line did not start a zellij session; "
            f"log={_reader_log(reader.log)!r}"
        ),
    )
    invocations = stub_log.read_text(encoding="utf-8")
    assert (
        "attach --create-background --force-run-commands --create demo" in invocations
    ), invocations
    assert "--layout" not in invocations, invocations

    # A name whose server is already live is not started again, and a listed
    # EXITED server does not count as one that is running.
    live = {**os.environ, **env, "RECKON_ZELLIJ_STUB_SESSIONS": "demo\n"}
    assert fleet_supervisor.session_running("demo", live) is True
    assert fleet_supervisor.session_running("other", live) is False
    exited = {**live, "RECKON_ZELLIJ_STUB_SESSIONS": "demo   EXITED\n"}
    assert fleet_supervisor.session_running("demo", exited) is False

    # Parsing splits each verb's fields on whitespace.
    assert fleet_supervisor.parse_request("session demo") == Request(
        "session", ("demo",)
    )
    assert fleet_supervisor.parse_request("session demo wide") == Request(
        "session", ("demo", "wide")
    )
    assert fleet_supervisor.parse_request("reload") == Request("reload", ())

    # Reload replaces the image in place, with no arguments, so the replacement
    # re-enters the request loop.
    recorded: list[list[str]] = []
    fleet_supervisor.handle_line(
        "reload",
        reader.runtime,
        os.environ,
        exec_=lambda path, argv: recorded.append(argv),
    )
    assert recorded == [MODULE_ARGV], recorded


def test_a_session_line_sizes_the_tabs_with_a_brief_client(reader, tmp_path) -> None:
    """A headless session's tabs are sized by a short-lived client.

    zellij 0.45 sizes each tab from the client that created it, so a session
    created with ``--create-background`` cannot lay out the tabs of a multi-tab
    layout: the server logs "Not enough room for panes" for each, and the first
    client to attach afterwards panics it. The verb therefore attaches one
    fixed-size client while the layout's tabs are created and takes it off once
    zellij reports them. The stub records every invocation, so the three steps
    and their order are observable without a real zellij.
    """
    stub_log = tmp_path / "zellij-argv.log"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "zellij"
    stub.write_text(ZELLIJ_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "RECKON_ZELLIJ_STUB_LOG": str(stub_log),
        "RECKON_ZELLIJ_STUB_SESSIONS": "",
        "RECKON_ZELLIJ_STUB_TAB_NAMES": "fleet\nrecord\nshell\nextra\n",
    }
    reader.start(env)
    _send(reader.runtime, "session demo fleet")
    invocations = _settled_argv(stub_log)
    assert invocations, (
        "the session line invoked zellij with nothing, so no order can be read "
        f"from it; log={_reader_log(reader.log)!r}"
    )

    # The ordered steps: create headless, attach the sized client, read the
    # tabs. The middle needle is the sized-client attach the declared mutation
    # removes, so the ordering assertion fails against it rather than a later
    # step, and the failure names the removed attach.
    lines = invocations.splitlines()
    cursor = 0
    for needle in (
        "--create-background --force-run-commands --create demo",
        "attach demo",
        "action query-tab-names",
    ):
        index = next(
            (i for i in range(cursor, len(lines)) if needle in lines[i]),
            None,
        )
        assert index is not None, (
            f"zellij was never invoked with {needle!r} in this order; "
            f"invocations={lines}"
        )
        cursor = index + 1

    # The size in the log is the pty's read-back, not the value the code meant
    # to hand the ioctl, so this fails if the window size is never set. The
    # companion case below reads the same value from the kernel directly.
    reader_log = _reader_log(reader.log)
    attached = re.search(
        r"sized client attached to demo on a (\d+)x(\d+) pty", reader_log
    )
    assert attached is not None, reader_log
    assert (int(attached.group(1)), int(attached.group(2))) == (200, 50), reader_log

    # The client must have ended, not merely been asked to: the status in this
    # line is the process's own exit code, and the stub's attach blocks, so a
    # detach that never signalled it would leave no status to report here.
    detached = re.search(
        r"sized client detached from demo \(exit (-?\d+)\); 4 tabs", reader_log
    )
    assert detached is not None, reader_log


def test_a_slow_layout_is_not_called_complete_after_its_first_tab(
    monkeypatch,
) -> None:
    """The tab wait holds until the list is quiet, not until two reads agree.

    A layout's tabs appear one at a time, roughly 1.5 s apart on the recording
    this behaviour comes from. A wait that returned as soon as two reads agreed
    would call a four-tab layout complete after its first tab, detach the sizing
    client, and leave the later tabs applied with no client attached — the
    defect the client exists to prevent. The source here adds a tab every 0.3 s,
    so equal reads happen long before the layout is done.
    """
    names = ["tab1", "tab2", "tab3", "tab4"]
    started = time.monotonic()

    def slow_tabs(name, environ=None) -> list[str]:
        grown = int((time.monotonic() - started) / 0.3)
        return names[: min(len(names), 1 + grown)]

    monkeypatch.setattr(fleet_supervisor, "tab_names", slow_tabs)
    read = fleet_supervisor._wait_for_tab_names("demo", {}, timeout=WAIT_SECONDS)
    assert read == names, read


def test_the_sized_client_pty_is_given_its_size() -> None:
    """The window size reaches the pty the client will use.

    Read back from the kernel rather than from the call, so a size that is
    requested and never applied is visible: with the ioctl dropped the pty
    keeps whatever size it was opened with, and the reader's own log line
    reports that size rather than 200x50.
    """
    master, slave = pty.openpty()
    try:
        applied = fleet_supervisor._set_pty_size(slave, 200, 50)
        kernel_rows, kernel_columns = struct.unpack(
            "HHHH", fcntl.ioctl(slave, termios.TIOCGWINSZ, bytes(8))
        )[:2]
    finally:
        os.close(slave)
        os.close(master)
    assert (kernel_columns, kernel_rows) == (200, 50), (kernel_columns, kernel_rows)
    assert applied == (200, 50), applied


def test_the_stripped_variables_are_absent_from_a_spawned_child(
    reader, tmp_path
) -> None:
    """The session variables this reader inherited never reach a spawned child."""
    run_directory = tmp_path / "run"
    dump = tmp_path / "child-environment.txt"
    stub = tmp_path / "dump.py"
    stub.write_text(ENV_DUMP_CHILD, encoding="utf-8")
    spec = _write_spec(run_directory, [sys.executable, str(stub), str(dump)])

    reader.start(
        {
            "ZELLIJ_SESSION_NAME": "inherited",
            "ZELLIJ_PANE_ID": "7",
            "CX_SESSION": "inherited",
            "CX_BACKGROUND": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "AI_AGENT": "claude-code",
            "GIT_EDITOR": "true",
            "ENVIRONMENT": "BATCH",
            "RECKON_CONTROL_MARKER": "present",
        }
    )
    _send(reader.runtime, f"spawn r-test {spec}")
    names = _wait_for(
        lambda: (
            dump.read_text(encoding="utf-8").split()
            if dump.exists() and dump.read_text(encoding="utf-8").split()
            else None
        ),
        message=(
            "the spawned child recorded no environment, so nothing can be said "
            "about what it inherited"
        ),
    )
    # The instrument must be shown to see a variable that is present before an
    # absence from it, or a child that recorded nothing would read as clean.
    assert "RECKON_CONTROL_MARKER" in names, names
    for stripped in (
        "ZELLIJ_SESSION_NAME",
        "ZELLIJ_PANE_ID",
        "CX_SESSION",
        "CX_BACKGROUND",
        "CLAUDE_CODE_CHILD_SESSION",
        "AI_AGENT",
        "GIT_EDITOR",
        "ENVIRONMENT",
    ):
        assert stripped not in names, f"{stripped} reached a spawned child: {names}"


# A child that records its own soft task limit and exits.
NPROC_DUMP_CHILD = (
    "import resource, sys\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write("
    "str(resource.getrlimit(resource.RLIMIT_NPROC)[0]))\n"
)

# A stand-in for the node sampler: it records the runtime and job it was
# started with, then exits, so no sampler outlives the case.
SAMPLER_STUB = """#!/bin/sh
printf 'XDG_RUNTIME_DIR=%s\\nFLEET_JOB_ID=%s\\n' "$XDG_RUNTIME_DIR" "$FLEET_JOB_ID" \\
    > "$RECKON_SAMPLER_MARKER"
"""


def _seed_record(state: Path, *, job: str, node: str) -> None:
    """A record left by an earlier start of the fleet, as a requeue finds it."""
    state.mkdir(parents=True, exist_ok=True)
    (state / fleet_supervisor.RECORD_NAME).write_text(
        json.dumps(
            {
                "job_id": job,
                "node": node,
                "runtime_dir": "/tmp/earlier-fleet",  # noqa: S108 - fixture value
                "started_at": "2026-09-27T19:55:51Z",
            }
        ),
        encoding="utf-8",
    )


def _published(state: Path) -> dict:
    try:
        return json.loads((state / fleet_supervisor.RECORD_NAME).read_text())
    except (OSError, ValueError):
        return {}


def test_a_restart_on_another_node_is_announced(reader) -> None:
    """A record naming this job on another node leaves a notice and an alert.

    That record is the only trace a requeue leaves: the batch log is truncated
    and the lost node refuses SSH, so the notice is what tells the operator
    every session there died, and where the node's last samples are.
    """
    _seed_record(reader.state, job="4242", node="lost-node")
    reader.start({"SLURM_JOB_ID": "4242"})
    notice = (reader.state / "notice").read_text(encoding="utf-8")
    assert "fleet job 4242 restarted on" in notice, notice
    assert "after losing lost-node" in notice, notice
    assert "4242-lost-node.tsv" in notice, notice
    alerts = (reader.state / "alerts.log").read_text(encoding="utf-8")
    assert alerts.strip() == notice.strip(), alerts
    # The notice is read from the old record, and the new record replaces it.
    assert _published(reader.state)["node"] == fleet_supervisor._short_hostname()


@pytest.mark.parametrize(
    ("job", "node"),
    [("4242", ""), ("9999", "lost-node")],
    ids=["same-job-same-node", "another-job"],
)
def test_a_start_that_is_not_a_requeue_leaves_no_notice(reader, job, node) -> None:
    """The same job on the same node, or another job, is not a requeue."""
    _seed_record(reader.state, job=job, node=node or fleet_supervisor._short_hostname())
    reader.start({"SLURM_JOB_ID": "4242"})
    # The reader publishes its own record only after deciding, so a record
    # naming this start shows the decision has been taken.
    published = _published(reader.state)
    assert published["job_id"] == "4242", published
    assert published["node"] == fleet_supervisor._short_hostname(), published
    assert not (reader.state / "notice").exists()
    assert not (reader.state / "alerts.log").exists()


def test_the_task_ceiling_reaches_a_spawned_child(reader, tmp_path) -> None:
    """A worker the batch step spawns inherits the fleet's soft task limit.

    The ceiling asked for is below the limit this test runs under, so a child
    reporting it shows the reader lowered the limit rather than passing on its
    own.
    """
    soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
    ceiling = 4321 if soft == resource.RLIM_INFINITY else min(4321, soft - 1)
    run_directory = tmp_path / "run"
    dump = tmp_path / "nproc.txt"
    stub = tmp_path / "nproc.py"
    stub.write_text(NPROC_DUMP_CHILD, encoding="utf-8")
    spec = _write_spec(run_directory, [sys.executable, str(stub), str(dump)])

    reader.start({"FLEET_NPROC": str(ceiling)})
    _send(reader.runtime, f"spawn r-test {spec}")
    reported = _wait_for(
        lambda: dump.read_text(encoding="utf-8") if dump.exists() else None,
        message="the spawned child recorded no task limit",
    )
    assert int(reported) == ceiling, (reported, ceiling)


def test_the_node_sampler_starts_in_the_fleet_runtime(reader, tmp_path) -> None:
    """The sampler named by the environment starts with the fleet's runtime."""
    marker = tmp_path / "sampler-ran"
    stub = tmp_path / "sampler"
    stub.write_text(SAMPLER_STUB, encoding="utf-8")
    stub.chmod(0o755)
    reader.start(
        {
            "FLEET_HEALTH_SAMPLER": str(stub),
            "RECKON_SAMPLER_MARKER": str(marker),
            "SLURM_JOB_ID": "4242",
        }
    )
    recorded = _wait_for(
        lambda: marker.read_text(encoding="utf-8") if marker.exists() else None,
        message=f"the sampler never ran; log={_reader_log(reader.log)!r}",
    )
    assert f"XDG_RUNTIME_DIR={reader.runtime}" in recorded, recorded
    assert "FLEET_JOB_ID=4242" in recorded, recorded


def test_a_resurrection_runs_its_commands_unless_turned_off(reader, tmp_path) -> None:
    """With the switch off, a session starts without forcing its commands."""
    stub_log = tmp_path / "zellij-argv.log"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "zellij"
    stub.write_text(ZELLIJ_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = {
        "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        "RECKON_ZELLIJ_STUB_LOG": str(stub_log),
        "RECKON_ZELLIJ_STUB_SESSIONS": "",
        "RECKON_ZELLIJ_STUB_TAB_NAMES": "main\n",
        "FLEET_FORCE_RUN_COMMANDS": "0",
    }
    reader.start(env)
    _send(reader.runtime, "session demo")
    _wait_for(
        lambda: "attach --create-background --create demo" in _argv_log(stub_log),
        message=f"no session start was recorded; log={_reader_log(reader.log)!r}",
    )
    assert "--force-run-commands" not in _argv_log(stub_log)


# A zellij whose queries never return, as a tab-name query against a session
# deleted while it was being started was measured to do. ``exec`` makes the
# sleeping process the one a timeout kills, so nothing holds the captured pipe
# open afterwards.
HANGING_QUERY_STUB = """#!/bin/sh
case "$1" in
  list-sessions) exec sleep 301 ;;
  action) exec sleep 301 ;;
esac
exit 0
"""


def _hanging_zellij(tmp_path: Path) -> dict[str, str]:
    stub_bin = tmp_path / "hanging-bin"
    stub_bin.mkdir()
    stub = stub_bin / "zellij"
    stub.write_text(HANGING_QUERY_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return {**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"}


def test_a_zellij_query_that_never_returns_is_abandoned(tmp_path, monkeypatch) -> None:
    """A hung query reads as no answer within its bound, so the reader moves on.

    Unbounded, one query against a vanished session held the batch step's only
    request reader for a day. Each call is now abandoned at its bound, so the
    tab wait's own deadline is reached rather than never checked.
    """
    monkeypatch.setattr(fleet_supervisor, "ZELLIJ_QUERY_SECONDS", 0.5)
    env = _hanging_zellij(tmp_path)
    started = time.monotonic()
    assert fleet_supervisor.tab_names("demo", env) == []
    assert fleet_supervisor.session_running("demo", env) is False
    names = fleet_supervisor._wait_for_tab_names("demo", env, settle=0.1, timeout=1.0)
    elapsed = time.monotonic() - started
    assert names == []
    assert elapsed < 10.0, f"three bounded calls and a 1 s wait took {elapsed:.1f}s"


def test_a_session_start_that_overruns_does_not_hold_the_reader(
    tmp_path, monkeypatch, capsys
) -> None:
    """The request loop stops a start copy that outlives its bound, and returns."""
    monkeypatch.setattr(fleet_supervisor, "SESSION_START_SECONDS", 2.0)
    env = {
        **_hanging_zellij(tmp_path),
        "FLEET_RUNTIME_DIR": str(tmp_path / "runtime"),
        "FLEET_STATE_DIR": str(tmp_path / "state"),
        "PYTHONPATH": REPO_ROOT,
    }
    started = time.monotonic()
    fleet_supervisor.handle_line("session demo", tmp_path / "runtime", env)
    elapsed = time.monotonic() - started
    assert elapsed < 20.0, f"the reader was held for {elapsed:.1f}s"
    assert "session start for demo outlived" in capsys.readouterr().out
    # The zellij call the copy was blocked on went with it, not left orphaned.
    uid = str(os.getuid())
    leftover = _wait_for(
        lambda: (
            subprocess.run(
                ["pgrep", "-u", uid, "-xf", "sleep 301"],
                capture_output=True,
                check=False,
            ).returncode
            != 0
        ),
        message="the stopped start copy left its blocked zellij call running",
        timeout=10.0,
    )
    assert leftover


def test_stopping_the_reader_stops_the_service_its_config_declared(
    reader, tmp_path
) -> None:
    """A declared service ends when the reader that started it is stopped.

    The stub is declared in the services file this case's isolated config home
    resolves, so the case observes both halves at once: a stub that never starts
    shows the reader read some other services file, and a stub that survives the
    stop shows the reader's end does not reach what it started. Survivors are
    read from each process's own environment rather than from its command line,
    so the assertion names the process by what it was handed.
    """
    reckon_home = tmp_path / "reckon-home"
    marker = tmp_path / "stub-started"
    config_home = Path(os.environ[fleet_supervisor.CONFIG_HOME_ENV])
    services_file = (
        config_home
        / fleet_supervisor.CONFIG_DIRECTORY_NAME
        / fleet_supervisor.SERVICES_FILE_NAME
    )
    services_file.parent.mkdir(parents=True, exist_ok=True)
    services_file.write_text(
        json.dumps({"stub": [sys.executable, "-c", STUB_SERVICE, str(marker)]}),
        encoding="utf-8",
    )

    reader.start({"RECKON_HOME": str(reckon_home)})
    _wait_for(
        marker.exists,
        message=(
            "the declared stub never started, so the reader did not read the "
            f"services file this case wrote; log={_reader_log(reader.log)!r}"
        ),
    )
    # The instrument is shown seeing a process that is there before an absence
    # is read from it. The reader itself carries the marker, so the case waits
    # for a second process besides it: the service it started.
    reader_pid = reader.opened[0][0].pid
    _wait_for(
        lambda: (
            [
                pid
                for pid in _processes_with_env("RECKON_HOME", str(reckon_home))
                if pid != reader_pid
            ]
            or None
        ),
        message=(
            "no process but the reader carries this case's RECKON_HOME, so the "
            "scan would read the same before and after the stop"
        ),
    )

    reader.stop()

    survivors = _processes_with_env("RECKON_HOME", str(reckon_home))
    if survivors:
        deadline = time.monotonic() + WAIT_SECONDS
        while survivors and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            survivors = _processes_with_env("RECKON_HOME", str(reckon_home))
    assert survivors == [], (
        f"processes carrying RECKON_HOME={reckon_home} outlived the reader's "
        f"stop: {survivors}"
    )


def test_the_operator_services_file_is_never_opened(tmp_path, monkeypatch) -> None:
    """The reader resolves its services file in the case's tree, not the operator's.

    The operator's home is simulated under this case's temporary directory — the
    real one is exactly what must not be touched — and its config declares a
    service, so a reader that fell back to it would both read the file and start
    something. Opens are recorded from the path objects the reader's own loader
    reads through: the isolated file is shown read, and the operator-shaped one
    is shown not to be.
    """
    operator_home = tmp_path / "operator-home"
    operator_services = (
        operator_home
        / ".config"
        / fleet_supervisor.CONFIG_DIRECTORY_NAME
        / fleet_supervisor.SERVICES_FILE_NAME
    )
    operator_services.parent.mkdir(parents=True, exist_ok=True)
    operator_services.write_text(
        json.dumps({"operator-stub": [sys.executable, "-c", "pass"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(operator_home))

    opened: list[str] = []
    real_open = Path.open

    def recording_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)

    isolated = fleet_supervisor.services_config_path(os.environ)
    assert isolated.is_relative_to(tmp_path), (
        f"the reader resolves its services file at {isolated}, outside this "
        "case's temporary directory"
    )
    isolated.parent.mkdir(parents=True, exist_ok=True)
    isolated.write_text(
        json.dumps({"isolated-stub": [sys.executable, "-c", "pass"]}),
        encoding="utf-8",
    )
    declared = fleet_supervisor.declared_services(os.environ)
    assert declared == {"isolated-stub": [sys.executable, "-c", "pass"]}, declared
    assert str(isolated) in opened, (
        "the reader's own services file was not read, so this instrument is not "
        "shown seeing a read it should see"
    )
    assert str(operator_services) not in opened, (
        f"the operator-shaped services file at {operator_services} was read"
    )
