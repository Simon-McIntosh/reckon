"""The fleet batch step reads requests and starts what each one names.

The reader is exercised over a real FIFO in a temporary runtime directory,
against the module's own ``python -m`` entry point, because the property under
measure is what a request line does to the machine: a spawned child exists, a
session start reaches zellij, the inherited session variables do not reach the
child. Each case therefore observes a process or a file rather than a return
value, and every absence is preceded by showing the same instrument reading a
known-present value.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
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

# A child that proves it ran and then stays alive, so the pid the reader
# records can be read while the process it names is still there.
LIVE_CHILD = (
    "import sys, time\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write('ran\\n')\n"
    "time.sleep(60)\n"
)

# A child that records the environment it was handed and exits.
ENV_DUMP_CHILD = (
    "import os, sys\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
    "    handle.write('\\n'.join(sorted(os.environ)))\n"
)

# A stand-in for zellij: it appends its argv to the file named by
# RECKON_ZELLIJ_STUB_LOG, prints the sessions named by
# RECKON_ZELLIJ_STUB_SESSIONS when asked to list them, and prints the tabs
# named by RECKON_ZELLIJ_STUB_TAB_NAMES when asked for the tab names. The names
# deliberately avoid the ZELLIJ prefix, which the reader strips from every
# environment it passes on, so the stub would lose its own configuration.
# Nothing is left to the real zellij for the reader to start a session on this
# machine.
ZELLIJ_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$RECKON_ZELLIJ_STUB_LOG"
case "$1" in
  list-sessions) printf '%s' "$RECKON_ZELLIJ_STUB_SESSIONS" ;;
  action) printf '%s' "$RECKON_ZELLIJ_STUB_TAB_NAMES" ;;
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

    yield SimpleNamespace(
        start=start, runtime=runtime, state=state, log=log, opened=opened
    )
    for process, _handle in opened:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
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
        lambda: "attach --create-background --create demo" in _argv_log(stub_log),
        message=(
            "the session line did not start a zellij session; "
            f"log={_reader_log(reader.log)!r}"
        ),
    )
    invocations = stub_log.read_text(encoding="utf-8")
    assert "attach --create-background --create demo" in invocations, invocations
    assert "--layout" not in invocations, invocations

    # A name whose server is already live is not started again, and a listed
    # EXITED server does not count as one that is running.
    live = {**os.environ, **env, "RECKON_ZELLIJ_STUB_SESSIONS": "demo\n"}
    assert fleet_supervisor.session_running("demo", live) is True
    assert fleet_supervisor.session_running("other", live) is False
    exited = {**live, "RECKON_ZELLIJ_STUB_SESSIONS": "demo   EXITED\n"}
    assert fleet_supervisor.session_running("demo", exited) is False

    # Parsing matches the reference's field split for both verbs.
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
        "--create-background --create demo",
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

    # The read reported every tab the layout declares, and the client was gone
    # by the time the session line returned, so the session is left headless.
    reader_log = _reader_log(reader.log)
    assert "sized client attached to demo on a 200x50 pty" in reader_log, reader_log
    assert "sized client detached from demo; 4 tabs" in reader_log, reader_log


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
    ):
        assert stripped not in names, f"{stripped} reached a spawned child: {names}"
