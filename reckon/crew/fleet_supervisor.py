"""The fleet allocation's batch step, carried in reckon.

A step started with ``srun`` ends when its login-side client does, so the batch
step is the only process tree on the fleet node that outlives every login node.
Anything that must survive a lost login node is started from here: the zellij
servers that hold the agent sessions, and the per-run supervisors that hold a
dispatched worker's parentage so its exit can be collected after its dispatcher
is gone.

The node has no ``/run/user/<uid>``, and a step cannot create one, so this makes
a private node-local runtime directory and every fleet process uses it for
``XDG_RUNTIME_DIR``: zellij's sockets and the agent harness's cross-session
sockets both live there.

Requests arrive one per line on a FIFO in that directory:

  ``session <name> [layout]``  start a zellij server for ``<name>`` unless one
                               is already running
  ``reload``                   re-execute this module in place, so a fix to it
                               takes effect without a new allocation
  ``spawn <run-id> <spec>``    run the supervisor spec's argv as this batch
                               step's own detached child, and acknowledge it in
                               the spec's run directory

Each session start runs a fresh copy of this module (the ``start`` mode), for
the same reason the reference did: the loop is long-lived, the starting logic is
not.

Where the fleet lives is published to ``record.json`` so the connection side can
resolve the job id rather than anyone remembering it. Both the record directory
(``$FLEET_STATE_DIR``, default ``~/.local/state/fleet``) and the private runtime
directory (``$FLEET_RUNTIME_DIR``, default ``/tmp/<uid>-fleet``) are overridable
so a harness can drive this reader against a temporary tree instead of the
machine's own state.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Mapping, MutableMapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNTIME_DIR_ENV = "FLEET_RUNTIME_DIR"
STATE_DIR_ENV = "FLEET_STATE_DIR"
DEFAULT_RUNTIME_SUFFIX = "-fleet"
DEFAULT_STATE_PARTS = (".local", "state", "fleet")

REQUEST_FIFO_NAME = "requests"
RECORD_NAME = "record.json"
SPAWNED_RECORD_NAME = "spawned.json"
SUPERVISOR_STDERR_NAME = "supervisor.stderr.log"
START_LOG_NAME = "zellij-start.log"

START_MODE = "start"

# A session name and a layout name both reach a process argument, and the layout
# name is used as a path under the zellij configuration directory. Both are
# restricted to what the reference accepted, and nothing wider.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# The submitting shell's environment is inherited by the batch step. An
# environment carrying these makes a descendant believe it is already inside a
# session: a zellij server refuses to create one, and an agent harness believes
# it is a child session, saves no transcript, and holds another session's
# sockets. The families are dropped so a variable added later is covered without
# this list being edited.
STRIPPED_PREFIXES = ("ZELLIJ", "CX_", "CLAUDE")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _short_hostname() -> str:
    return socket.gethostname().split(".")[0]


def log(message: str) -> None:
    print(f"[{_utc_now()}] {message}", flush=True)


def stripped_variables(environ: Mapping[str, str]) -> list[str]:
    """Name the inherited variables the fleet must not pass on."""
    return [name for name in environ if name.startswith(STRIPPED_PREFIXES)]


def strip_inherited_variables(environ: MutableMapping[str, str]) -> list[str]:
    """Drop the inherited session variables in place, and name what was dropped."""
    removed = stripped_variables(environ)
    for name in removed:
        environ.pop(name, None)
    return removed


def runtime_directory(environ: Mapping[str, str] | None = None) -> Path:
    """The private node-local runtime directory this batch step owns."""
    environ = os.environ if environ is None else environ
    override = environ.get(RUNTIME_DIR_ENV)
    if override:
        return Path(override)
    return Path("/tmp") / f"{os.getuid()}{DEFAULT_RUNTIME_SUFFIX}"  # noqa: S108 - node-local


def state_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Where the fleet publishes the record naming where it lives."""
    environ = os.environ if environ is None else environ
    override = environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home().joinpath(*DEFAULT_STATE_PARTS)


def prepare_runtime(environ: MutableMapping[str, str] | None = None) -> Path:
    """Create the private runtime directory and point the fleet at it.

    The directory is mode 0700 because it holds both zellij's sockets and the
    agent harness's cross-session sockets, which are reachable by anyone who can
    walk to them.
    """
    environ = os.environ if environ is None else environ
    runtime = runtime_directory(environ)
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    socket_dir = runtime / "zellij"
    socket_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(socket_dir, 0o700)
    environ["XDG_RUNTIME_DIR"] = str(runtime)
    environ["ZELLIJ_SOCKET_DIR"] = str(socket_dir)
    # Node-local scratch is pinned rather than taken from TMPDIR: the allocator
    # may point TMPDIR at shared storage, and the fleet's runtime sockets must
    # live on the node that holds them.
    environ["TMPDIR"] = "/tmp"  # noqa: S108 - the node's own tmp, never shared
    environ["FLEET_JOB_ID"] = environ.get("SLURM_JOB_ID", "")
    return runtime


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically, so a reader never sees a half-written record.

    The parent directory is never created here: a run directory a discard has
    removed must stay removed, so a write into one that is gone is a deliberate
    refusal rather than a resurrection.
    """
    descriptor, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def publish_record(runtime: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Publish where the fleet lives, so the connection side can resolve it."""
    environ = os.environ if environ is None else environ
    state = state_directory(environ)
    state.mkdir(parents=True, exist_ok=True)
    record = {
        "job_id": environ.get("SLURM_JOB_ID", ""),
        "node": _short_hostname(),
        "runtime_dir": str(runtime),
        "started_at": _utc_now(),
    }
    path = state / RECORD_NAME
    _write_json(path, record)
    return path


def session_running(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a live zellij server already serves ``name``.

    A listed ``EXITED`` server is not running: zellij keeps exited sessions in
    ``list-sessions`` output, so a name whose only server has exited must be
    started afresh rather than reported as already up.
    """
    try:
        result = subprocess.run(
            ["zellij", "list-sessions", "--no-formatting"],
            capture_output=True,
            text=True,
            check=False,
            env=None if environ is None else dict(environ),
        )
    except OSError:
        return False
    for line in result.stdout.splitlines():
        if "EXITED" in line:
            continue
        fields = line.split()
        if fields and fields[0] == name:
            return True
    return False


def start_session(
    name: str,
    layout: str,
    runtime: Path,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Start a zellij server for ``name`` unless one is already running."""
    if not SAFE_NAME.fullmatch(name or ""):
        log(f"refused session name: {name}")
        return 1
    if layout and not SAFE_NAME.fullmatch(layout):
        log(f"refused layout: {layout}")
        return 1
    if session_running(name, environ):
        log(f"session {name} already running")
        return 0
    suffix = f" with layout {layout}" if layout else ""
    log(f"starting zellij session {name}{suffix}")
    argv = ["zellij"]
    if layout:
        argv += ["--layout", layout]
    argv += ["attach", "--create-background", "--create", name]
    with open(runtime / START_LOG_NAME, "ab") as started:
        result = subprocess.run(
            argv,
            cwd=str(Path.home()),
            stdout=started,
            stderr=started,
            check=False,
            env=None if environ is None else dict(environ),
        )
    if result.returncode != 0:
        log(f"zellij start failed for {name}")
        return 1
    return 0


def spawn_supervisor(
    run_id: str,
    spec_path: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run the spec's argv as this batch step's own detached child.

    The child leads its own session and process group, mirroring the per-run
    supervisor the dispatcher would otherwise fork: one group signal reaches the
    worker beneath it, while the ending of a session step cannot, so the run
    outlives the coordinator that asked for the dispatch.

    The acknowledgement is written only after the child exists, so a reader that
    finds ``spawned.json`` is reading the pid of a process that was started
    rather than an intention to start one.
    """
    environ = os.environ if environ is None else environ
    spec_file = Path(spec_path)
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    if not isinstance(spec, Mapping):
        raise TypeError(f"supervisor spec {spec_path!r} is not a mapping")
    argv = [str(argument) for argument in spec["argv"]]
    run_directory = Path(str(spec.get("run_directory") or spec_file.parent))
    with open(run_directory / SUPERVISOR_STDERR_NAME, "ab") as errors:
        process = subprocess.Popen(
            argv,
            cwd=spec.get("cwd") or None,
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
        )
    record = {"pid": process.pid, "started_at": _utc_now()}
    _write_json(run_directory / SPAWNED_RECORD_NAME, record)
    log(f"spawned supervisor for {run_id} as pid {process.pid}")
    return record


@dataclass(frozen=True)
class Request:
    """One parsed request line: the verb and the fields after it."""

    verb: str
    fields: tuple[str, ...]


def parse_request(line: str) -> Request:
    """Split a request line the way the reference's ``read`` did."""
    fields = line.split()
    if not fields:
        return Request("", ())
    return Request(fields[0], tuple(fields[1:]))


def _reexec(exec_: Any) -> None:
    """Replace this image with a fresh copy, so a fix to the module takes hold.

    No arguments are passed, so the replacement enters the request loop again
    and republishes its record and FIFO, exactly as the reference's bare
    ``exec "$0"`` did.
    """
    argv = [sys.executable, "-m", "reckon.crew.fleet_supervisor"]
    exec_(argv[0], argv)


def _run_session_copy(name: str, layout: str, environ: Mapping[str, str] | None) -> int:
    """Start a session through a fresh copy of this module."""
    argv = [sys.executable, "-m", "reckon.crew.fleet_supervisor", START_MODE, name]
    if layout:
        argv.append(layout)
    return subprocess.run(
        argv, check=False, env=None if environ is None else dict(environ)
    ).returncode


def handle_line(
    line: str,
    runtime: Path,
    environ: Mapping[str, str] | None = None,
    exec_: Any = os.execv,
) -> None:
    """Act on one request line; a line this reader cannot act on is logged only."""
    request = parse_request(line)
    if request.verb == "session":
        layout = request.fields[1] if len(request.fields) > 1 else ""
        _run_session_copy(request.fields[0] if request.fields else "", layout, environ)
    elif request.verb == "reload":
        log("reloading")
        _reexec(exec_)
    elif request.verb == "spawn" and len(request.fields) == 2:
        run_id, spec_path = request.fields
        try:
            spawn_supervisor(run_id, spec_path, environ)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log(f"spawn failed for {run_id}: {type(exc).__name__}: {exc}")
    elif request.verb == "":
        return
    else:
        log(f"unknown request: {line.strip()}")


def _reap_finished_children() -> None:
    """Collect any exited child so a long-lived reader holds no dead slot.

    The spawned supervisor is this batch step's child and exits once the run it
    holds is over; without this the reader, whose whole life is a read loop,
    would hold one process-table slot per completed run. It is called between
    requests and never during a synchronous child wait, so it cannot race the
    session copy's own collection.
    """
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def serve(
    environ: MutableMapping[str, str] | None = None,
    *,
    exec_: Any = os.execv,
) -> int:
    """Read requests from the FIFO until the batch step ends.

    Nothing here terminates the loop: the batch step is meant to outlive every
    session on the node, so the loop ends only when the allocation does or when
    a ``reload`` replaces this image.
    """
    environ = os.environ if environ is None else environ
    strip_inherited_variables(environ)
    runtime = prepare_runtime(environ)
    publish_record(runtime, environ)
    fifo = runtime / REQUEST_FIFO_NAME
    with suppress(FileNotFoundError):
        fifo.unlink()
    os.mkfifo(fifo, 0o600)
    log(
        f"fleet supervisor on {_short_hostname()}, "
        f"job {environ.get('SLURM_JOB_ID') or '?'}, runtime {runtime}"
    )
    # Opening read-write holds a write end open, so a read between requests
    # blocks for the next line instead of seeing end-of-file.
    descriptor = os.open(fifo, os.O_RDWR)
    with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
        while True:
            _reap_finished_children()
            line = stream.readline()
            if not line:
                continue
            handle_line(line, runtime, environ, exec_)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Enter either the one-shot session start or the request loop."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ
    if arguments and arguments[0] == START_MODE:
        strip_inherited_variables(environ)
        runtime = prepare_runtime(environ)
        name = arguments[1] if len(arguments) > 1 else ""
        layout = arguments[2] if len(arguments) > 2 else ""
        return start_session(name, layout, runtime, environ)
    return serve(environ)


if __name__ == "__main__":
    raise SystemExit(main())
