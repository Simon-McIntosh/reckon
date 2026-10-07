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
                               takes effect without a new allocation; the
                               declared services are applied first, so a newly
                               named service starts and a removed one stops
  ``spawn <run-id> <spec>``    run the supervisor spec's argv as this batch
                               step's own detached child, and acknowledge it in
                               the spec's run directory
  ``stop``                     stop every declared service this reader started
                               and end the request loop, so nothing it started
                               outlives it
  ``promote``                  publish the fleet record and start declared
                               services when this reader began in standby

Each session start runs a fresh copy of this module (the ``start`` mode),
because the loop is long-lived and the starting logic is not: a fix to how a
session starts takes effect at the next start without a reload.

Where the fleet lives is published to ``record.json`` so the connection side can
resolve the job id rather than anyone remembering it. Both the record directory
(``$FLEET_STATE_DIR``, default ``~/.local/state/fleet``) and the private runtime
directory (``$FLEET_RUNTIME_DIR``, default ``/tmp/<uid>-fleet``) are overridable
so a harness can drive this reader against a temporary tree instead of the
machine's own state.

A user config file, ``<config home>/fleet/services.json``, may declare standing
services as service names mapped to argv lists. Each declared service is started
as this batch step's own-session child behind a lock in the runtime directory,
restarted with backoff when it exits, and stopped on reload by the pid recorded
for it; each one's output is appended to a bounded log under the state
directory. The config home follows ``$XDG_CONFIG_HOME`` when the environment
names one, so a harness can point the configuration at a temporary tree too.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import resource
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import service
from reckon._store import write_json_atomically
from reckon.crew.host_lease import LEASE_RENEW_SECONDS, HostLease
from reckon.crew.routing import signal_worker

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

# The request loop reads its FIFO with a bounded wait rather than a blocking
# read, so the sweep for exited children runs on its own interval instead of
# only when a request happens to arrive. Requests are rare and the loop's whole
# life is a read, so a child that exits while the FIFO is idle would otherwise
# stay defunct until the next request arrives. The interval is short enough that
# an exited child is collected within a second of its own exit, and it costs one
# wakeup per interval.
REQUEST_WAIT_SECONDS = 0.5
REQUEST_READ_BYTES = 65536

# A session created with ``--create-background`` applies its layout with no
# client's size to lay it out in, and zellij 0.45 sizes each tab from the
# client that created it rather than from the terminal a later client brings.
# The tabs that the background creation cannot fit are left broken, and the
# next client to attach to one panics the server. One client on a pty of a
# fixed size is enough to give every tab a size, so it is attached while the
# tabs are created and taken off again once they exist.
#
# Removable when zellij sizes each tab from the client that attaches to it
# rather than from the background client that created the session.
SIZED_CLIENT_COLUMNS = 200
SIZED_CLIENT_ROWS = 50
# A layout's tabs do not all appear at once: the recorded application on this
# machine created tab 1 at 02:51:58.781 and tab 3 at 02:52:01.869, roughly 1.5 s
# apart. Two equal reads one poll apart would therefore call a layout complete
# after its first tab, detach the client, and leave the remaining tabs applied
# with no client attached — the defect this sizing client exists to prevent. The
# tab list is held unchanged for a settle window longer than that spacing
# instead, and the whole wait is still bounded so a layout whose tabs never
# appear cannot hold the batch step's reader open.
TAB_POLL_SECONDS = 0.05
TAB_SETTLE_SECONDS = 2.5
TAB_WAIT_SECONDS = 30.0
DETACH_GRACE_SECONDS = 5.0

# Every zellij command this module runs is bounded. The batch step reads its
# requests one at a time, so a single zellij call that never returns stops every
# later session start on the node: measured on the fleet, a tab-name query
# against a session deleted while it was being sized slept for a day, and each
# session asked for afterwards timed out in fleet-attach. A query answers in
# well under a second, and a create returns once the server is up; past these
# bounds the call is abandoned and read as no answer.
ZELLIJ_QUERY_SECONDS = 10.0
ZELLIJ_CREATE_SECONDS = 60.0
# A whole session start: the create, the tab wait, one query past its deadline,
# and the client's detach, with room to spare. The request loop kills a start
# copy that outlives it, so one stuck start cannot hold the reader.
SESSION_START_SECONDS = 180.0

# A session name and a layout name both reach a process argument, and the layout
# name is used as a path under the zellij configuration directory. Both are
# restricted to a plain name, and nothing wider.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# The submitting shell's environment is inherited by the batch step. An
# environment carrying these makes a descendant believe it is already inside a
# session: a zellij server refuses to create one, and an agent harness believes
# it is a child session, saves no transcript, and holds another session's
# sockets. The families are dropped so a variable added later is covered without
# this list being edited.
STRIPPED_PREFIXES = ("ZELLIJ", "CX_", "CLAUDE")

# Single variables an agent's tool shell or the batch step sets, none of which
# belongs in an interactive pane: GIT_EDITOR=true makes a `git commit` without
# -m accept the default message unseen, AI_AGENT tells every CLI in the pane an
# agent is driving, and SLURM sets ENVIRONMENT for a batch step.
STRIPPED_NAMES = (
    "AI_AGENT",
    "GIT_EDITOR",
    "COREPACK_ENABLE_AUTO_PIN",
    "NoDefaultCurrentDirectoryInExePath",
    "ENVIRONMENT",
)

# A ceiling on this user's task count, set as the batch step's soft
# RLIMIT_NPROC so every zellij server, pane and worker inherits it. A spawn
# loop anywhere on the node grows until memory runs out, slurmd stops
# answering, and SLURM fails the node with every session on it; at the ceiling
# the loop's fork fails first and a chain of waiting processes unwinds. A full
# fleet uses about 2,500 tasks.
NPROC_ENV = "FLEET_NPROC"
DEFAULT_NPROC = 6000

# The node sampler this batch step starts, found on PATH. It writes one line a
# minute to shared storage, which is the only record of a node's last minutes
# once SLURM has failed it. An empty value starts none.
HEALTH_SAMPLER_ENV = "FLEET_HEALTH_SAMPLER"
DEFAULT_HEALTH_SAMPLER = "fleet-health"

# A session resurrected after the fleet restarts on another node parks every
# pane at "Waiting to run" until someone presses Enter in it. Resurrecting with
# --force-run-commands runs each pane's recorded command instead, and a pane
# started through fleet-claude records its conversation id, so each resumes its
# own conversation. It reruns every recorded command, not only agent sessions,
# so a false value turns it off. A session created rather than resurrected is
# unaffected.
FORCE_RUN_ENV = "FLEET_FORCE_RUN_COMMANDS"
_OFF = frozenset({"0", "false", "no"})

# Services a user config declares, and the mode one copy of a service runs in.
# The variable the config directory resolves under takes its name from the base
# resolution in ``reckon.service``, so the two cannot drift apart.
CONFIG_HOME_ENV = service.XDG_CONFIG_HOME_ENV
CONFIG_DIRECTORY_NAME = "fleet"
SERVICES_FILE_NAME = "services.json"
SERVICE_MODE = "service"
SERVICE_LOG_DIRECTORY_NAME = "services"
SERVICE_LOCK_SUFFIX = ".lock"
SERVICE_PID_SUFFIX = ".pid"

# A declared service that exits is restarted after a delay that starts here and
# doubles to the cap. A run this long is steady, so the sequence starts over
# rather than waiting out a cap the service earned long ago.
FIRST_BACKOFF_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 300.0
STEADY_RUN_SECONDS = 600.0

# The supervision tick notices an exited service and starts what its backoff has
# come due. A long tick is the service itself working, never a reason to act:
# the consuming project measured a cold catch-up tick of 862 s, and a service is
# restarted only when it exits, never on a liveness timeout.
SUPERVISION_TICK_SECONDS = 1.0
SERVICE_STOP_GRACE_SECONDS = 10.0
SERVICE_POLL_SECONDS = 0.1

# The log lives on the shared filesystem so it survives the node. It is bounded
# rather than left to grow: past the limit the oldest bytes are dropped and the
# newest kept, cut at a line boundary, so a later reader still has the last ticks.
SERVICE_LOG_LIMIT_BYTES = 8 * 1024 * 1024
SERVICE_LOG_KEEP_BYTES = 4 * 1024 * 1024

ALERTS_NAME = "alerts.log"
NOTICE_NAME = "notice"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _short_hostname() -> str:
    return socket.gethostname().split(".")[0]


def log(message: str) -> None:
    print(f"[{_utc_now()}] {message}", flush=True)


def stripped_variables(environ: Mapping[str, str]) -> list[str]:
    """Name the inherited variables the fleet must not pass on."""
    return [
        name
        for name in environ
        if name.startswith(STRIPPED_PREFIXES) or name in STRIPPED_NAMES
    ]


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

    The parent directory is never created: a run directory a discard has removed
    must stay removed, so a write into one that is gone is a deliberate refusal
    rather than a resurrection. That is why the shared writer is asked not to
    create the parent.
    """
    write_json_atomically(
        path,
        payload,
        indent=2,
        sort_keys=True,
        fsync=False,
        mode=0o600,
        create_parents=False,
    )


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


def requeue_notice(environ: Mapping[str, str] | None = None) -> str | None:
    """The notice for an allocation restarted on another node, or None.

    A requeued allocation keeps its job id and starts again elsewhere, so a
    record naming this job on a different node means every session on that node
    died. Nothing else says so: the requeue truncates the batch log, and the
    lost node refuses SSH once no job of this user runs there. Read before this
    start publishes its own record over the old one.
    """
    environ = os.environ if environ is None else environ
    job = environ.get("SLURM_JOB_ID", "")
    state = state_directory(environ)
    try:
        previous = json.loads((state / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(previous, Mapping):
        return None
    lost = str(previous.get("node") or "")
    node = _short_hostname()
    if not job or str(previous.get("job_id") or "") != job or not lost or lost == node:
        return None
    return (
        f"{_utc_now()} fleet job {job} restarted on {node} after losing {lost}. "
        f"Every session on {lost} died; each pane started through fleet-claude "
        "reruns its own conversation, and sessions.tsv in the fleet state "
        "directory maps pane to conversation for any that do not. Last node "
        f"samples: {state / 'health' / f'{job}-{lost}.tsv'}. Check the cause "
        f"with: sacct -j {job} -D -o JobID,State,Start,End,NodeList"
    )


def announce_requeue(environ: Mapping[str, str] | None = None) -> str | None:
    """Record a requeue where the next attach shows it, and return the notice."""
    environ = os.environ if environ is None else environ
    notice = requeue_notice(environ)
    if notice is None:
        return None
    state = state_directory(environ)
    with open(state / ALERTS_NAME, "a", encoding="utf-8") as alerts:
        alerts.write(notice + "\n")
    (state / NOTICE_NAME).write_text(notice + "\n", encoding="utf-8")
    log(notice)
    return notice


def apply_task_ceiling(environ: Mapping[str, str] | None = None) -> int:
    """Lower this process's soft task limit to the fleet ceiling, and return it.

    Every descendant inherits the soft limit. An existing limit tighter than the
    ceiling is kept, and the hard limit is never touched.
    """
    environ = os.environ if environ is None else environ
    try:
        ceiling = int(str(environ.get(NPROC_ENV) or DEFAULT_NPROC))
    except ValueError:
        ceiling = DEFAULT_NPROC
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    if hard != resource.RLIM_INFINITY:
        ceiling = min(ceiling, hard)
    if soft != resource.RLIM_INFINITY and soft <= ceiling:
        return soft
    resource.setrlimit(resource.RLIMIT_NPROC, (ceiling, hard))
    return ceiling


def start_health_sampler(environ: Mapping[str, str] | None = None) -> int | None:
    """Start the node sampler as this batch step's own child, and return its pid.

    The sampler holds a lock in the runtime directory, so a reload that calls
    this again starts a copy that exits at once rather than a second sampler.
    It leads its own session, so it ends with the allocation and with nothing
    else.
    """
    environ = os.environ if environ is None else environ
    name = environ.get(HEALTH_SAMPLER_ENV, DEFAULT_HEALTH_SAMPLER)
    if not name:
        return None
    sampler = shutil.which(name, path=environ.get("PATH"))
    if sampler is None:
        log(f"no node sampler: {name} is not on PATH")
        return None
    process = subprocess.Popen(
        [sampler],
        env=dict(environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log(f"node sampler {sampler} started as pid {process.pid}")
    return process.pid


def config_directory(environ: Mapping[str, str] | None = None) -> Path:
    """The user's fleet configuration directory, ``~/.config/fleet``.

    The XDG config base is resolved once, in :mod:`reckon.service`; this
    appends the directory's own name to it. A caller that isolates
    ``XDG_CONFIG_HOME`` drives the whole configuration path without reaching
    the operator's own.
    """
    return service.xdg_config_home(environ) / CONFIG_DIRECTORY_NAME


def services_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """The file declaring which services the fleet runs."""
    return config_directory(environ) / SERVICES_FILE_NAME


def service_log_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Where a declared service's log is appended.

    Under the state directory rather than the config home: the state directory
    is on the shared filesystem, so a later reader can open the last ticks after
    the node that ran the service is gone.
    """
    return state_directory(environ) / SERVICE_LOG_DIRECTORY_NAME


def declared_services(
    environ: Mapping[str, str] | None = None,
) -> dict[str, list[str]] | None:
    """The services the user's config declares, or None if it cannot be read.

    A file that is absent declares nothing. A file that is present but cannot
    be parsed is refused as a whole, so a caller applies no half-read file: the
    services already running are left alone rather than stopped on a misread.
    An entry naming an unsafe name or carrying no argv is refused on its own,
    and the rest of the file still applies.
    """
    path = services_config_path(environ)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log(f"services config {path} unreadable: {type(exc).__name__}: {exc}")
        return None
    try:
        raw = json.loads(text)
    except ValueError as exc:
        log(f"services config {path} is not JSON: {exc}")
        return None
    if not isinstance(raw, Mapping):
        log(f"services config {path} is {type(raw).__name__}, not a mapping")
        return None
    services: dict[str, list[str]] = {}
    for name, argv in raw.items():
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            log(f"refused service name: {name!r}")
            continue
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(argument, str) for argument in argv)
        ):
            log(f"refused service {name}: argv is not a non-empty string list")
            continue
        services[name] = list(argv)
    return services


def lock_held(name: str, runtime: Path) -> bool:
    """Whether a copy of the service holds its lock, i.e. is running.

    The lock is the service's own, taken by the wrapper before it becomes the
    service and dropped by the kernel when the service ends. It is therefore
    held for exactly as long as a copy runs, which is what a restart decision
    and a stop both read.
    """
    path = runtime / f"{name}{SERVICE_LOCK_SUFFIX}"
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def service_pid_path(runtime: Path, name: str) -> Path:
    """The file recording the pid of the running copy of a service."""
    return runtime / f"{name}{SERVICE_PID_SUFFIX}"


def recorded_service_pid(runtime: Path, name: str) -> int | None:
    """The pid recorded for a service, or None when none could be read."""
    try:
        return int(service_pid_path(runtime, name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def next_backoff(delay: float, ran_seconds: float) -> tuple[float, float]:
    """The delay before a restart, and the one that follows it.

    Five seconds at first, doubling to the cap. A run of steady seconds or more
    starts the sequence over, so a service that ran steadily and then exited
    waits five seconds rather than a cap it reached long ago.
    """
    if ran_seconds >= STEADY_RUN_SECONDS:
        delay = FIRST_BACKOFF_SECONDS
    return delay, min(delay * 2, BACKOFF_CAP_SECONDS)


def trim_service_log(path: Path, *, limit: int, keep: int) -> bool:
    """Bound a service log, keeping its newest bytes and dropping the oldest.

    The service appends to the file directly, so the file is edited in place
    rather than replaced: the same inode is kept and its newest bytes are moved
    down to the front, under an open descriptor the running service still writes
    through. The cut is moved forward to the next line boundary, so the first
    line a later reader sees is a whole one. An absent log is not a failure.
    Returns whether anything was trimmed.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= limit:
        return False
    keep = min(keep, limit)
    with open(path, "rb+") as handle:
        handle.seek(max(size - keep, 0))
        tail = handle.read()
        boundary = tail.find(b"\n")
        if boundary != -1:
            tail = tail[boundary + 1 :]
        handle.seek(0)
        handle.write(tail)
        handle.truncate()
    return True


def service_wrapper_argv(name: str, argv: Sequence[str]) -> list[str]:
    """The command that starts one service copy, taking its lock first.

    The copy runs through this module's service mode, so the lock is taken by
    the same process that becomes the service: it survives the exec and is held
    for exactly the service's life. The supervisor names no service itself --
    the argv is whatever the user's config declared.
    """
    return [
        sys.executable,
        "-m",
        "reckon.crew.fleet_supervisor",
        SERVICE_MODE,
        name,
        *argv,
    ]


@dataclass
class _ServiceState:
    """What the supervisor knows about one declared service."""

    argv: list[str]
    pid: int | None = None
    owned: bool = False
    delay: float = FIRST_BACKOFF_SECONDS
    next_attempt: float | None = None
    started_at: float | None = None
    last_renewed: float | None = None


class DeclaredServices:
    """The services a user config declares, run as children of the batch step.

    Each copy leads its own session, so it ends with the allocation and with
    nothing else. Liveness is read from the service's own lock rather than from
    a keepalive: a long tick is the service working, and only an exit is a
    reason to restart. A copy that cannot take its lock exits at once, so a
    reload or a second supervisor never starts a second copy of a service.

    Time enters through ``clock`` so a caller can drive backoff without
    sleeping; the batch step passes its own monotonic clock by default.
    """

    def __init__(
        self,
        runtime: Path,
        environ: Mapping[str, str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        log_limit: int = SERVICE_LOG_LIMIT_BYTES,
        log_keep: int = SERVICE_LOG_KEEP_BYTES,
    ) -> None:
        self._runtime = runtime
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._log_limit = log_limit
        self._log_keep = log_keep
        self._states: dict[str, _ServiceState] = {}
        self._leases: dict[str, HostLease] = {}
        self._host = _short_hostname()
        self._guard = threading.Lock()

    def _lease(self, name: str) -> HostLease:
        lease = self._leases.get(name)
        if lease is None:
            lease = HostLease(
                state_directory(self._environ),
                name,
                self._host,
                os.getpid(),
                self._environ.get("FLEET_JOB_ID")
                or self._environ.get("SLURM_JOB_ID", ""),
            )
            self._leases[name] = lease
        return lease

    def reload(self) -> None:
        """Apply the declared services: start newly named, stop removed ones.

        Run at startup and on the verb that re-executes the module. A service
        already running is adopted rather than started again, so an exec does
        not disturb what it runs. A config that cannot be read changes nothing,
        so a half-read file never stops every service.
        """
        declared = declared_services(self._environ)
        if declared is None:
            return
        with self._guard:
            for name in list(self._states):
                if name not in declared:
                    self._stop_locked(name)
            for name, argv in declared.items():
                state = self._states.get(name)
                if state is None:
                    state = _ServiceState(argv=list(argv))
                    self._states[name] = state
                else:
                    state.argv = list(argv)
                if state.pid is not None or state.next_attempt is not None:
                    continue
                lease = self._lease(name)
                if not lease.claim():
                    holder = lease.holder()
                    if holder is not None:
                        log(
                            f"declared service {name} held by {holder.host} "
                            f"pid {holder.pid} job {holder.job or '?'}"
                        )
                    state.next_attempt = self._clock() + LEASE_RENEW_SECONDS
                    continue
                pid = recorded_service_pid(self._runtime, name)
                if pid is not None and lock_held(name, self._runtime):
                    state.pid = pid
                    state.owned = False
                    state.started_at = self._clock()
                    state.last_renewed = self._clock()
                    log(f"declared service {name} already running as pid {pid}")
                    continue
                service_pid_path(self._runtime, name).unlink(missing_ok=True)
                self._start_locked(name, state.argv)

    def start(self, name: str, argv: Sequence[str]) -> int | None:
        """Start one copy of a service through its lock-taking wrapper."""
        with self._guard:
            return self._start_locked(name, argv)

    def _start_locked(self, name: str, argv: Sequence[str]) -> int | None:
        """Spawn the wrapper that takes the lock and becomes the service."""
        if not SAFE_NAME.fullmatch(name or "") or not argv:
            log(f"refused service start: {name!r}")
            return None
        lease = self._lease(name)
        if not lease.claim():
            holder = lease.holder()
            if holder is not None:
                log(
                    f"declared service {name} held by {holder.host} "
                    f"pid {holder.pid} job {holder.job or '?'}"
                )
            return None
        log_path = service_log_directory(self._environ) / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "ab") as sink:
                process = subprocess.Popen(
                    service_wrapper_argv(name, list(argv)),
                    env=dict(self._environ),
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=sink,
                    start_new_session=True,
                )
        except OSError as exc:
            lease.release()
            log(f"declared service {name} could not start: {exc}")
            return None
        service_pid_path(self._runtime, name).write_text(
            f"{process.pid}\n", encoding="utf-8"
        )
        log(f"declared service {name} started as pid {process.pid}")
        state = self._states.get(name)
        if state is None:
            state = _ServiceState(argv=list(argv))
            self._states[name] = state
        state.argv = list(argv)
        state.pid = process.pid
        state.owned = True
        state.started_at = self._clock()
        state.last_renewed = self._clock()
        state.next_attempt = None
        return process.pid

    def stop(self, name: str) -> None:
        """Stop one service by the pid recorded for it, if it is running."""
        with self._guard:
            self._stop_locked(name)

    def stop_all(self) -> None:
        """Stop every service this supervisor is running, and wait for each end.

        The stop path of the reader: what it started does not outlive it. Each
        name is stopped by its recorded pid, the same way a reload stops one the
        config no longer declares, and the guard is held across the sweep so the
        supervision thread cannot restart a copy between two stops.
        """
        with self._guard:
            for name in list(self._states):
                self._stop_locked(name)

    def _stop_locked(self, name: str) -> None:
        """Signal the recorded pid, and wait for the service to end.

        Nothing is signalled unless the service's lock is held: a recorded pid
        whose process has already gone names nothing this supervisor started,
        and the lock frees only when the service ends. A copy that does not
        end within the grace is killed, still by that pid and never by pattern.
        """
        state = self._states.pop(name, None)
        lease = self._leases.pop(name, None)
        pid_path = service_pid_path(self._runtime, name)
        pid = state.pid if state is not None else None
        if pid is None:
            pid = recorded_service_pid(self._runtime, name)
        if not lock_held(name, self._runtime):
            pid_path.unlink(missing_ok=True)
            if lease is not None:
                lease.release()
            return
        if pid is None:
            log(f"declared service {name} runs with no recorded pid; not stopped")
            return
        log(f"stopping declared service {name} (pid {pid})")
        signal_worker(pid, signal.SIGTERM, reason="fleet-service-stop")
        deadline = time.monotonic() + SERVICE_STOP_GRACE_SECONDS
        while lock_held(name, self._runtime):
            if time.monotonic() >= deadline:
                log(f"declared service {name} did not stop; killing pid {pid}")
                signal_worker(pid, signal.SIGKILL, reason="fleet-service-stop")
                break
            time.sleep(SERVICE_POLL_SECONDS)
        self._collect_locked(pid)
        pid_path.unlink(missing_ok=True)
        if lease is not None:
            lease.release()

    def _collect_locked(self, pid: int) -> None:
        """Collect a child this image started, so it leaves no dead slot."""
        for _ in range(20):
            try:
                reaped, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if reaped == pid:
                return
            time.sleep(SERVICE_POLL_SECONDS)

    def supervise_once(self, now: float | None = None) -> None:
        """Re-check every service once: restart what exited, trim its log.

        A service is restarted only once its lock is free, which the kernel
        arranges when the service ends. Nothing here times a service out: a
        long tick is the service working, so a service's silence is never a
        reason to restart it, and only a backoff that has come due starts a
        new copy.
        """
        now = self._clock() if now is None else now
        with self._guard:
            for name, state in list(self._states.items()):
                lease = self._leases.get(name)
                if (
                    state.pid is not None
                    and lease is not None
                    and (
                        state.last_renewed is None
                        or now - state.last_renewed >= LEASE_RENEW_SECONDS
                    )
                ):
                    if not lease.renew():
                        log(f"declared service {name} lost its shared lease; stopping")
                        self._stop_locked(name)
                        continue
                    state.last_renewed = now
                log_path = service_log_directory(self._environ) / f"{name}.log"
                trim_service_log(log_path, limit=self._log_limit, keep=self._log_keep)
                if state.pid is not None and not self._alive(name, state):
                    ran = (
                        now - state.started_at if state.started_at is not None else 0.0
                    )
                    delay, following = next_backoff(state.delay, ran)
                    state.delay = following
                    state.pid = None
                    state.owned = False
                    state.started_at = None
                    state.last_renewed = None
                    if lease is not None:
                        lease.release()
                    state.next_attempt = now + delay
                    log(f"declared service {name} exited; restart in {delay:.0f}s")
                if state.next_attempt is not None and now >= state.next_attempt:
                    state.next_attempt = None
                    if self._start_locked(name, state.argv) is None:
                        delay, following = next_backoff(state.delay, 0.0)
                        state.delay = following
                        state.next_attempt = now + delay

    def supervise_loop(self, stop: threading.Event) -> None:
        """Tick until told to stop, so a service that exits comes back."""
        while not stop.wait(SUPERVISION_TICK_SECONDS):
            self.supervise_once()

    def _alive(self, name: str, state: _ServiceState) -> bool:
        """Whether a copy of the service is still running.

        A child this image started is checked by waitpid, which also collects
        it; the check falls back to the service's lock when another reader has
        already collected it. The recorded pid alone is never the answer: the
        lock is what a running copy holds and what the kernel drops when it
        ends.
        """
        pid = state.pid
        if pid is None:
            return False
        if state.owned:
            try:
                reaped, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                if reaped == pid:
                    return False
                if reaped == 0:
                    return True
        return lock_held(name, self._runtime)


def run_declared_service(
    name: str, argv: Sequence[str], environ: Mapping[str, str] | None = None
) -> int:
    """Become one copy of a declared service, unless a copy already runs.

    The copy holds an exclusive lock in the runtime directory for its whole
    life -- the same lock the supervisor probes to know that the service is up.
    A copy that cannot take it exits at once, so a reload or a second
    supervisor never starts a second copy. The lock is held on a descriptor
    that survives the exec, and the kernel drops it when the service ends.
    """
    environ = os.environ if environ is None else environ
    if not SAFE_NAME.fullmatch(name or "") or not argv:
        log(f"refused declared service: {name!r}")
        return 2
    runtime = runtime_directory(environ)
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / f"{name}{SERVICE_LOCK_SUFFIX}"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"declared service {name} already running; this copy exits")
        os.close(descriptor)
        return 1
    os.set_inheritable(descriptor, True)
    os.execvp(argv[0], list(argv))  # noqa: S606 - replaces this image, no shell
    raise AssertionError("execvp returned")


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
            timeout=ZELLIJ_QUERY_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in result.stdout.splitlines():
        if "EXITED" in line:
            continue
        fields = line.split()
        if fields and fields[0] == name:
            return True
    return False


def tab_names(name: str, environ: Mapping[str, str] | None = None) -> list[str]:
    """The names of a session's tabs, read without attaching a client.

    ``zellij action`` targets the session named by ``ZELLIJ_SESSION_NAME``, so
    this runs from the batch step where no client exists to inherit a session
    from. An unreadable or absent answer is an empty list rather than an error:
    the caller waits on the names appearing, and a probe that cannot see them
    is the same reading as a session that has none yet.
    """
    child_environ = dict(os.environ if environ is None else environ)
    child_environ["ZELLIJ_SESSION_NAME"] = name
    try:
        result = subprocess.run(
            ["zellij", "action", "query-tab-names"],
            capture_output=True,
            text=True,
            check=False,
            env=child_environ,
            timeout=ZELLIJ_QUERY_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _wait_for_tab_names(
    name: str,
    environ: Mapping[str, str] | None = None,
    *,
    settle: float = TAB_SETTLE_SECONDS,
    timeout: float = TAB_WAIT_SECONDS,
) -> list[str]:
    """Wait until the tab list has been quiet for a settle window.

    A layout's tabs appear one at a time, roughly 1.5 s apart on the recording
    this behaviour is drawn from, so a list read twice one poll apart can be
    equal while the layout still has tabs to create. The list must therefore be
    non-empty and unchanged for a settle window longer than that spacing before
    the layout is treated as applied. The whole wait is bounded, so a layout
    whose tabs never appear cannot hold the batch step's reader open.
    """
    deadline = time.monotonic() + timeout
    settled_at: float | None = None
    previous: list[str] = []
    while True:
        names = tab_names(name, environ)
        now = time.monotonic()
        if names != previous:
            previous = names
            settled_at = now if names else None
        elif settled_at is not None and now - settled_at >= settle:
            return names
        if now >= deadline:
            return names
        time.sleep(TAB_POLL_SECONDS)


def _drain_pty(fd: int) -> None:
    """Discard what the sized client writes, so a full pty buffer cannot block it."""
    with suppress(OSError):
        while os.read(fd, 65536):
            pass


def _set_pty_size(fd: int, columns: int, rows: int) -> tuple[int, int]:
    """Set a pty's window size and read it back, so the applied size is known.

    The read-back is what the kernel holds for the terminal the client will use,
    which is what the sizing depends on. Reporting the numbers that were asked
    for would state the size this meant to set whether or not it took effect.
    """
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    applied_rows, applied_columns = struct.unpack(
        "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8))
    )[:2]
    return applied_columns, applied_rows


def _detach_client(
    client: subprocess.Popen, *, grace: float = DETACH_GRACE_SECONDS
) -> int | None:
    """Take the sized client off the session, and report how it ended.

    The status is read after the wait rather than inferred from having called
    this function, so a caller reports that the client has gone rather than that
    it was asked to go. It is ``None`` only if the client outlived both the
    terminate and the kill, which a caller should treat as a detach that did not
    happen.
    """
    if client.poll() is None:
        client.terminate()
        try:
            client.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            client.kill()
            with suppress(subprocess.TimeoutExpired):
                client.wait(timeout=grace)
    return client.poll()


def size_tabs_with_a_client(
    name: str,
    environ: Mapping[str, str] | None = None,
    *,
    columns: int = SIZED_CLIENT_COLUMNS,
    rows: int = SIZED_CLIENT_ROWS,
) -> list[str]:
    """Give a headless session's tabs a size by attaching one client briefly.

    zellij 0.45 sizes every tab from the client that created it, so a session
    created with ``attach --create-background`` and a multi-tab layout has tabs
    it cannot lay out: the server logs "Not enough room for panes" for each one,
    and the first client to attach afterwards panics it. Attaching one client on
    a pty of a fixed size while the layout's tabs are created gives every tab a
    size, and detaching it leaves the session headless again. The tab names are
    read back through a second channel, so a caller learns what the session
    holds without holding a client open on it.

    The client's own terminal is a pty this function owns, so its detach is its
    termination rather than a keystroke the session's keybindings would have to
    be trusted to honour. The pty's size is read back after it is set, so what
    the log reports is the size the tabs were laid out in.
    """
    child_environ = dict(os.environ if environ is None else environ)
    child_environ.pop("ZELLIJ_SESSION_NAME", None)
    master, slave = pty.openpty()
    columns, rows = _set_pty_size(slave, columns, rows)
    try:
        client = subprocess.Popen(
            ["zellij", "attach", name],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env=child_environ,
        )
    except BaseException:
        os.close(slave)
        os.close(master)
        raise
    os.close(slave)
    threading.Thread(target=_drain_pty, args=(master,), daemon=True).start()
    log(f"sized client attached to {name} on a {columns}x{rows} pty")
    names: list[str] = []
    status: int | None = None
    try:
        names = _wait_for_tab_names(name, child_environ)
    finally:
        status = _detach_client(client)
        with suppress(OSError):
            os.close(master)
    log(f"sized client detached from {name} (exit {status}); {len(names)} tabs")
    return names


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
    argv += ["attach", "--create-background"]
    if str((environ or os.environ).get(FORCE_RUN_ENV, "1")).lower() not in _OFF:
        argv.append("--force-run-commands")
    argv += ["--create", name]
    with open(runtime / START_LOG_NAME, "ab") as started:
        try:
            result = subprocess.run(
                argv,
                cwd=str(Path.home()),
                stdout=started,
                stderr=started,
                check=False,
                env=None if environ is None else dict(environ),
                timeout=ZELLIJ_CREATE_SECONDS,
            )
        except subprocess.TimeoutExpired:
            log(
                f"zellij start for {name} did not return in {ZELLIJ_CREATE_SECONDS:.0f}s"
            )
            return 1
    if result.returncode != 0:
        log(f"zellij start failed for {name}")
        return 1
    size_tabs_with_a_client(name, environ)
    return 0


def _spawn_environment(
    spec: Mapping[str, Any], environ: Mapping[str, str]
) -> dict[str, str]:
    """The environment to start the spec's argv with.

    The batch step's own environment is the base, and the spec's carried
    ``environment`` is applied over it. A dispatch resolves the run it starts
    through reckon's crew home, so a supervisor started under the batch step's
    home rather than the dispatcher's finds no run and exits at once -- after
    the spawn has been acknowledged, which is the defect this carries the
    environment to close. A spec whose carried environment is not a mapping, or
    whose entries are not name/string pairs, is refused rather than passed on,
    so a malformed spec cannot smuggle a value the child would misread.
    """
    child = dict(environ)
    carried = spec.get("environment")
    if carried is None:
        return child
    if not isinstance(carried, Mapping):
        raise TypeError(
            f"supervisor spec environment is {type(carried).__name__}, not a mapping"
        )
    for name, value in carried.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError(
                f"supervisor spec environment entry {name!r} is not a string pair"
            )
        child[name] = value
    return child


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

    The child starts with this batch step's environment overlaid by the spec's
    carried ``environment``, so a supervisor dispatched with a non-default crew
    home resolves the run under that home rather than this step's own.

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
    child_environ = _spawn_environment(spec, environ)
    with open(run_directory / SUPERVISOR_STDERR_NAME, "ab") as errors:
        process = subprocess.Popen(
            argv,
            cwd=spec.get("cwd") or None,
            env=child_environ,
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
    """Split a request line on whitespace into its verb and fields."""
    fields = line.split()
    if not fields:
        return Request("", ())
    return Request(fields[0], tuple(fields[1:]))


def _reexec(exec_: Any, *, standby: bool = False) -> None:
    """Replace this image with a fresh copy, so a fix to the module takes hold.

    A standby reader retains standby across the replacement, so a reload before
    promotion cannot publish the fleet record or start declared services.
    """
    argv = [sys.executable, "-m", "reckon.crew.fleet_supervisor"]
    if standby:
        argv.append("--standby")
    exec_(argv[0], argv)


def _run_session_copy(name: str, layout: str, environ: Mapping[str, str] | None) -> int:
    """Start a session through a fresh copy of this module, bounded.

    The copy leads its own process group, so a copy that outlives
    :data:`SESSION_START_SECONDS` is stopped together with the zellij call it
    is blocked on: SIGTERM first, which the copy turns into an ordinary exit so
    its sizing client is detached, then SIGKILL for whatever remains. A zellij
    server the copy created leads a session of its own and is not in the group.
    """
    argv = [sys.executable, "-m", "reckon.crew.fleet_supervisor", START_MODE, name]
    if layout:
        argv.append(layout)
    process = subprocess.Popen(
        argv,
        env=None if environ is None else dict(environ),
        start_new_session=True,
    )
    try:
        return process.wait(timeout=SESSION_START_SECONDS)
    except subprocess.TimeoutExpired:
        log(
            f"session start for {name} outlived {SESSION_START_SECONDS:.0f}s "
            "and was stopped"
        )
        with suppress(ProcessLookupError):
            signal_worker(process.pid, signal.SIGTERM, reason="session-start-timeout")
        try:
            process.wait(timeout=DETACH_GRACE_SECONDS * 2)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                signal_worker(
                    process.pid, signal.SIGKILL, reason="session-start-timeout"
                )
            process.wait()
        return 1


def handle_line(
    line: str,
    runtime: Path,
    environ: Mapping[str, str] | None = None,
    exec_: Any = os.execv,
    services: DeclaredServices | None = None,
    *,
    standby: bool = False,
    promote: Callable[[], None] | None = None,
) -> bool:
    """Act on one request line; a line this reader cannot act on is logged only.

    Returns whether the reader keeps reading requests. Only the ``stop`` verb
    returns False, so the loop ends when it is asked to rather than on any line
    the reader could not act on.
    """
    request = parse_request(line)
    if request.verb == "session":
        layout = request.fields[1] if len(request.fields) > 1 else ""
        _run_session_copy(request.fields[0] if request.fields else "", layout, environ)
    elif request.verb == "reload":
        if services is not None and not standby:
            services.reload()
        log("reloading")
        _reexec(exec_, standby=standby)
    elif request.verb == "promote" and not request.fields:
        if promote is not None:
            promote()
    elif request.verb == "spawn" and len(request.fields) == 2:
        run_id, spec_path = request.fields
        try:
            spawn_supervisor(run_id, spec_path, environ)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log(f"spawn failed for {run_id}: {type(exc).__name__}: {exc}")
    elif request.verb == "stop":
        log("stopping")
        return False
    elif request.verb == "":
        return True
    else:
        log(f"unknown request: {line.strip()}")
    return True


def _reap_finished_children() -> None:
    """Collect any exited child so a long-lived reader holds no dead slot.

    The spawned supervisor is this batch step's child and exits once the run it
    holds is over; without this the reader, whose whole life is a read loop,
    would hold one process-table slot per completed run. It is called on the
    reader's own interval, so a child that exits while the FIFO is idle is
    collected without a request arriving to prompt it. It never runs during a
    synchronous child wait, so it cannot race the session copy's own collection.
    """
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def readable_within(descriptor: int, timeout: float) -> bool:
    """Whether the descriptor has a line ready within the bound.

    The request loop waits here instead of in a blocking read, so the interval
    on which it can collect an exited child is bounded even when no request
    arrives. A descriptor the wait cannot watch reads as not ready, so a
    failure to wait is never mistaken for a request.
    """
    try:
        ready, _, _ = select.select([descriptor], [], [], timeout)
    except (OSError, ValueError):
        return False
    return bool(ready)


def serve(
    environ: MutableMapping[str, str] | None = None,
    *,
    exec_: Any = os.execv,
    standby: bool = False,
) -> int:
    """Read requests from the FIFO until the batch step ends.

    Nothing here terminates the loop: the batch step is meant to outlive every
    session on the node, so the loop ends only when the allocation does or when
    a ``reload`` replaces this image.
    """
    environ = os.environ if environ is None else environ
    strip_inherited_variables(environ)
    apply_task_ceiling(environ)
    runtime = prepare_runtime(environ)
    state_directory(environ).mkdir(parents=True, exist_ok=True)
    announce_requeue(environ)
    if not standby:
        publish_record(runtime, environ)
    fifo = runtime / REQUEST_FIFO_NAME
    pending_fifo = runtime / f".{REQUEST_FIFO_NAME}.pending"
    with suppress(FileNotFoundError):
        pending_fifo.unlink()
    os.mkfifo(pending_fifo, 0o600)
    # Publish the path only after holding its read end. A nonblocking writer
    # that sees the path can then open it immediately, even during startup.
    descriptor = os.open(pending_fifo, os.O_RDWR)
    os.replace(pending_fifo, fifo)
    log(
        f"fleet supervisor on {_short_hostname()}, "
        f"job {environ.get('SLURM_JOB_ID') or '?'}, runtime {runtime}"
    )
    start_health_sampler(environ)
    services = DeclaredServices(runtime, environ)
    if not standby:
        services.reload()
    is_standby = standby

    def promote() -> None:
        nonlocal is_standby
        if not is_standby:
            return
        publish_record(runtime, environ)
        is_standby = False
        services.reload()
        log("fleet supervisor promoted")

    stopping = threading.Event()
    threading.Thread(
        target=services.supervise_loop,
        args=(stopping,),
        name="fleet-services",
        daemon=True,
    ).start()
    # Opening read-write holds a write end open, so a read between requests
    # blocks for the next line instead of seeing end-of-file. The wait for that
    # line is bounded rather than blocking: the loop wakes on its own interval
    # so an exited child is collected without a request arriving, and reads
    # whichever whole lines the wakeup delivered.
    try:
        pending = b""
        while True:
            _reap_finished_children()
            if not readable_within(descriptor, REQUEST_WAIT_SECONDS):
                continue
            chunk = os.read(descriptor, REQUEST_READ_BYTES)
            if not chunk:
                continue
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                line = raw.decode("utf-8", "replace")
                if handle_line(
                    line,
                    runtime,
                    environ,
                    exec_,
                    services,
                    standby=is_standby,
                    promote=promote,
                ):
                    continue
                return 0
    finally:
        # The reader's end is the stop path for everything it started: a stop
        # request, and an exception escaping the loop, both stop the declared
        # services here. A reload never reaches this, because exec replaces the
        # image without unwinding, so the services it adopted keep running.
        with suppress(OSError):
            os.close(descriptor)
        stopping.set()
        services.stop_all()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Enter either the one-shot session start or the request loop."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ
    if arguments and arguments[0] == START_MODE:
        # A stop from the request loop ends the copy as an exit rather than a
        # kill, so the sizing client's detach in its finally block still runs.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
        strip_inherited_variables(environ)
        runtime = prepare_runtime(environ)
        name = arguments[1] if len(arguments) > 1 else ""
        layout = arguments[2] if len(arguments) > 2 else ""
        return start_session(name, layout, runtime, environ)
    if arguments and arguments[0] == SERVICE_MODE:
        name = arguments[1] if len(arguments) > 1 else ""
        return run_declared_service(name, arguments[2:], environ)
    return serve(environ, standby=arguments == ["--standby"])


if __name__ == "__main__":
    raise SystemExit(main())
