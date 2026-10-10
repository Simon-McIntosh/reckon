# ruff: noqa: I001
from __future__ import annotations

import fcntl
import os
import re
import shlex
import shutil
import socket
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from reckon._store import (
    _config_home,
)
from reckon.crew.host_lease import HostLease
from reckon.crew.node import (
    DEFAULT_WATCH_STALL_WINDOW,
    CrewError,
    parse_duration,
)


def watch_host_lease(project: str):
    """The shared-storage lease for a project's producer seat."""
    path = watch_lock_path(project)
    return HostLease(
        path.parent,
        path.stem,
        socket.gethostname(),
        os.getpid(),
        os.environ.get("SLURM_JOB_ID", ""),
    )


def _fresh_watch_holder(project: str, record: Mapping[str, Any]):
    """Use a remote lease, but clear a local holder confirmed dead by this host."""
    lease = watch_host_lease(project)
    holder = lease.holder()
    if holder is None or holder.host != socket.gethostname():
        return holder
    if (record.get("host"), record.get("pid")) == (holder.host, holder.pid):
        dead = record_process_alive(record) is False
    else:
        dead = process_alive(holder.pid) is False
    if dead:
        lease.release_holder(holder)
        return None
    return holder


def _seat_host(record: Mapping[str, Any]) -> str:
    """The host a seat record names as its producer's, or an empty string."""
    return str(record.get("host") or "")


def _seat_names_a_foreign_host(record: Mapping[str, Any]) -> bool:
    """Report whether a seat record names a host that is not this one.

    Distinct from :func:`_seat_host_is_local` because a record naming *no* host
    is neither: it predates the field, or it is an erased seat, and both are read
    differently from a seat known to belong elsewhere.
    """
    host = _seat_host(record)
    return bool(host) and host != socket.gethostname()


def _seat_stream_fresh(project: str, record: Mapping[str, Any]) -> bool:
    """Report whether a seat's transition stream moved within its stall window.

    The stream lies on the same shared home as the seat, so a line written to it
    recently is the one piece of evidence a reader on another host has that a
    producer it cannot see is still producing. An empty seat record names no
    window, so the default one is used.
    """
    window = record.get("stall_window") or DEFAULT_WATCH_STALL_WINDOW
    try:
        seconds = parse_duration(str(window))
    except (CrewError, TypeError, ValueError):
        seconds = parse_duration(DEFAULT_WATCH_STALL_WINDOW)
    try:
        written = watch_stream_path(project).stat().st_mtime
    except OSError:
        return False
    return (time.time() - written) <= seconds


def _record_producer_running(
    record: Mapping[str, Any], *, project: str | None = None
) -> bool:
    """Report whether a seat record names a producer that is live now.

    Drawn from the process table, not from the seat's held-state: a live
    producer whose record no longer holds the seat lock still reads as live,
    which is the direction a guard must not be fooled in. Deliberately not the
    start-time gate :func:`producer_live` applies — a running process is live
    whether or not its recorded start time still matches, because the
    start-time check exists for who may *signal* that process, a different
    question than whether it is running.

    The process table answers only for a record this host issued. A record
    naming another host is judged by its transition stream instead, which is the
    one piece of evidence that crosses the shared home. A record naming no host
    at all is an erased seat or a legacy one and is read against the local table,
    exactly as it always was: admission is a question about a producer, and a
    record that names no producer to admit answers nothing.

    ``project`` names the stream for a record that carries no project of its own.
    """
    if project:
        lease = watch_host_lease(project)
        holder = lease.holder()
        if holder is not None and holder.host != socket.gethostname():
            return True
        if (
            holder is None
            and lease.path.exists()
            and _seat_names_a_foreign_host(record)
        ):
            return False
    if _seat_names_a_foreign_host(record):
        return _stream_says_alive(record, project)
    pid = record.get("pid")
    return bool(pid) and record_process_alive(record, match_start_time=False) is True


def _seat_project(record: Mapping[str, Any], project: str | None) -> str:
    """The project whose stream a seat record is judged against."""
    if project:
        return str(project)
    return str(record.get("project") or "")


def _stream_says_alive(record: Mapping[str, Any], project: str | None) -> bool:
    """Whether a seat's stream is moving, for a record that carries no project."""
    name = _seat_project(record, project)
    return bool(name) and _seat_stream_fresh(name, record)


def watch_state(project: str, *, session: str | None = None) -> dict[str, Any]:
    """Return the paste-ready arming line and process-backed watcher liveness."""
    arming_line = _watch_arming_line(project)
    attach_line = _watch_attach_line(project, session=session)
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Delivery is reported beside the seat because reading one without the
    # other is how "a watcher is live" came to mean "I will be told".
    delivery = follower_state(project, session) if session is not None else None
    attached = None if delivery is None else bool(delivery["live"])
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            registration = _read_watch_record(handle)
        else:
            registration = _read_watch_record(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    # Liveness is reconciled against the process table rather than read from
    # the seat's held-state: a probe that sees the lock free reads a running
    # producer as absent, and one that sees it held reads a dead process as
    # live — each the wrong way to decide a dispatch guard. The running answer
    # is the one the guard may trust.
    holder = _fresh_watch_holder(project, registration)
    if holder is not None:
        if (registration.get("host"), registration.get("pid")) != (
            holder.host,
            holder.pid,
        ):
            registration = {"project": project}
        registration.update(host=holder.host, pid=holder.pid, job=holder.job)
    watcher_live = _record_producer_running(registration, project=project)
    return {
        "arming_line": arming_line,
        "attach_line": attach_line,
        "ensure_line": watcher_ensure_line(project),
        "unit": registration.get("unit"),
        "watcher_live": watcher_live,
        "watcher": dict(registration),
        "session": session,
        "session_attached": attached,
        # Whether this session armed a follower that has since been released,
        # as distinct from one it never armed. Only the released case may keep
        # dispatching on the strength of a live watcher process.
        "session_follower_released": bool(delivery and delivery["released"]),
        "follower": {} if delivery is None else delivery["follower"],
    }


# ── Watcher user service ────────────────────────────────────────────────────


WATCH_UNIT_TEMPLATE = """\
[Unit]
Description=reckon crew watcher for {project}
After=network.target

[Service]
Type=simple
WorkingDirectory={working_directory}
Environment="PATH={path}"
Environment="{unit_variable}={unit}"
{environment}\
ExecStart={exec_start}
StandardOutput=append:{log_file}
StandardError=append:{log_file}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _reckon_console_script() -> str:
    """Return the absolute path of the reckon console script to run.

    Both callers need the absolute path: the watcher unit runs without a shell,
    so its ExecStart cannot depend on PATH, and the follower attach line is
    armed by a shell that does not carry the interpreter's bin directory on
    PATH. The interpreter's own bin directory is preferred because it pins the
    caller to the environment the command was invoked from, and it is taken
    without resolving ``sys.executable`` because a virtualenv's interpreter is
    a symlink into the base distribution -- resolving it would leave the
    virtualenv, and the console script with it, behind.
    """
    sibling = Path(sys.executable).parent / "reckon"
    if sibling.is_file():
        return str(sibling)
    discovered = shutil.which("reckon")
    if discovered:
        return os.path.abspath(discovered)
    raise CrewError(
        "the 'reckon' console script is not beside the running interpreter "
        "and is not on PATH"
    )


def _watcher_search_path(config: Mapping[str, Any]) -> str:
    """Return the PATH a project's watcher must run with.

    The measured fault: a watcher unit ran seven hours with the backend
    directory absent from PATH, so every wait-lift it issued died at exec while
    the pointer kept reading ``working``. The PATH here is the one the launch
    would search — the resolved backend executables' own directories first —
    so a lift started by this watcher can resolve what a dispatch can.
    """
    from reckon.crew.dispatch import assert_routable_backends_resolvable

    resolved = assert_routable_backends_resolvable("<watcher>", config)
    directories = [str(Path(row["executable"]).parent) for row in resolved]
    current = (os.environ.get("PATH") or os.defpath).split(os.pathsep)
    return os.pathsep.join(
        dict.fromkeys(directory for directory in [*directories, *current] if directory)
    )


def _watcher_service_environment(config: Mapping[str, Any]) -> dict[str, str]:
    """Return the environment the watcher unit must carry."""
    environment = {"PATH": _watcher_search_path(config)}
    config_home = os.environ.get("RECKON_HOME")
    if config_home:
        # Forward the config home so the unit resolves the same mounts and run
        # pointers as the shell that ensured it, rather than the account
        # default it would otherwise fall back to.
        environment["RECKON_HOME"] = str(Path(config_home).expanduser().resolve())
    return environment


def render_watch_unit(
    project: str,
    *,
    environment: Mapping[str, str],
    executable: str | None = None,
) -> str:
    """Render the systemd user unit that runs one project's watcher."""
    from reckon import service

    return service.render_watch_unit(
        project=project,
        template=WATCH_UNIT_TEMPLATE,
        unit_name=watch_unit_name(project),
        unit_variable=WATCH_UNIT_ENV,
        log_file=watch_log_path(project),
        environment=environment,
        executable=executable or _reckon_console_script(),
    )


def _register_watch_unit(project: str, unit: str) -> dict[str, Any]:
    """Record the unit name in the project's watcher registration.

    Written only while the seat is free, and non-blocking: when the seat is held,
    the watcher holding it is authoritative and records the unit itself from
    its own environment, so a registration never ends up with no live writer
    behind it. A held seat is reported rather than overwritten.
    """
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = _read_watch_record(handle)
            return {
                "registered": False,
                "reason": "seat-held",
                "unit": held.get("unit") or unit,
            }
        record = _read_watch_record(handle)
        record["project"] = project
        record["unit"] = unit
        _write_watch_record(handle, record)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {"registered": True, "reason": "written", "unit": unit}


def _service_manager_unreachable(error: BaseException) -> bool:
    """Report whether an arming failure means the service bus cannot be reached.

    Only an unreachable manager is a reason to arm the watcher another way; a
    unit the manager refused on its merits must still raise, or the fallback
    would replace a diagnosis with a process nobody asked for. The markers are
    the ones systemd and logind print when the per-user manager is gone:
    ``Failed to connect to bus: Connection refused`` after a client restart,
    ``No such file or directory`` when the bus socket has been removed, and the
    launcher's own ``is not available on this host`` when it is absent entirely.
    """
    detail = str(error).lower()
    return any(
        marker in detail
        for marker in (
            "failed to connect to bus",
            "connection refused",
            "connection reset by peer",
            "transport endpoint is not connected",
            "is not available on this host",
        )
    )


def _arm_watcher_as_process(project: str) -> Mapping[str, Any]:
    """Start the project's watcher as a plain background process.

    The same producer the dispatch path arms, so the fallback reuses one watcher
    implementation rather than adding a second: it takes the seat once, replaces
    a seat whose supervisor has died, and reports liveness rather than raising.
    Imported lazily because the dispatch path imports this module.

    A process nothing supervises is a process whose death leaves no record, so
    the log a watcher keeps matters as much on this route as on the dispatch
    path's. Both go through :func:`dispatch._ensure_watch_producer`, which
    names the file the watcher's output belongs in on the argv it spawns, so
    this route names nothing itself and the two cannot drift.
    """
    from reckon.crew.dispatch import _ensure_watch_producer

    return _ensure_watch_producer(project)


def ensure_watcher_service(
    project: str,
    *,
    manager: Any | None = None,
    config: Mapping[str, Any] | None = None,
    restart: bool = False,
    producer: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Start or restart a project's watcher as an idempotent user service.

    Idempotent in the sense that decides whether a second call disturbs a live
    watcher: the unit is rewritten only when its rendered content changed, and a
    unit already active on an unchanged definition is reported rather than
    restarted. Restarting unconditionally would drop the seat and re-take it,
    so the command a refusal tells a person to run would interrupt the watcher
    it exists to guarantee.

    Arming also survives a manager that cannot be reached. The user manager is a
    single point of failure for a command whose whole job is to leave a watcher
    behind: a watcher started as a plain process lifts on every tick exactly as
    the service would, so a bus failure falls back to that path rather than
    raising and leaving the project unwatched. The result names which path armed
    the watcher, and why, in fields a caller reads rather than in prose.

    A host that will not enable lingering falls back the same way. Measured
    2026-09-25 on a fleet compute node, ``loginctl enable-linger`` answered
    ``Could not enable linger: No such device or address``; a unit that stops at
    logout is worse than no unit at all for a coordinator that has already been
    told a watcher is durable, so the watcher is placed in the session that owns
    it instead of raising. The failure is named in the `detail` sentence because
    the two causes of a fallback read differently to a person.

    The fallback is deliberately narrow — a unit the manager refused on its
    merits still raises — and ``producer`` is the seam that lets a caller supply
    a process arming, so a test exercises this path without starting a watcher.
    """
    from reckon import flight, service

    service_manager = (
        manager
        if manager is not None
        else service.SystemdUserUnitManager(watch_unit_name, watch_log_path)
    )
    if manager is None:
        # A real unit is written to the account's systemd directory, which a
        # throwaway configuration home must never cause. Imported lazily so the
        # read-only surfaces of this module do not depend on the arming path.
        from reckon.crew.dispatch import _refuse_arming_under_a_throwaway_home

        _refuse_arming_under_a_throwaway_home(project)
    resolved_config = (
        config if config is not None else flight.resolve(project=project).config
    )
    environment = _watcher_service_environment(resolved_config)
    # Rendering and writing the unit touch no bus, so a failure here is a real
    # one about the account's own filesystem and never a reason to fall back.
    content = render_watch_unit(project, environment=environment)
    path, changed = service_manager.write_unit(project, content)

    try:
        was_active = service_manager.active(project)
        # Lingering is settled before the unit starts, not after: a manager that
        # will not keep this account's units past logout is a reason to place the
        # watcher outside the manager, and settling this first keeps the seat free
        # for the process that then takes it. Checking after the start would leave
        # a service holding the seat that the fallback could not displace.
        lingering: bool | None = None
        if LINGER_IF_REQUIRED and hasattr(service_manager, "lingering"):
            lingering = bool(service_manager.lingering())
            if not lingering:
                service_manager.enable_linger()
                lingering = bool(service_manager.lingering())
        start_required = bool(changed or restart or not was_active)
        if start_required:
            # 'enable --now' leaves an already-running unit on its old
            # definition, so a rewritten active unit needs an explicit restart.
            service_manager.start(
                project, restart=bool(restart or (changed and was_active))
            )
    except Exception as error:
        from reckon import service

        if isinstance(error, service.LingerUnavailableError):
            return _watcher_armed_as_process(
                project,
                error=error,
                unit_path=path,
                unit_changed=bool(changed),
                environment=environment,
                producer=producer or _arm_watcher_as_process,
                cause=_LINGER_FALLBACK_CAUSE,
            )
        if not _service_manager_unreachable(error):
            raise
        return _watcher_armed_as_process(
            project,
            error=error,
            unit_path=path,
            unit_changed=bool(changed),
            environment=environment,
            producer=producer or _arm_watcher_as_process,
        )

    registration = _register_watch_unit(project, watch_unit_name(project))
    if start_required:
        detail = (
            f"restarted {watch_unit_name(project)} onto a rewritten unit"
            if changed and was_active
            else f"started {watch_unit_name(project)}"
        )
    else:
        detail = (
            f"{watch_unit_name(project)} is already active on an unchanged unit; "
            "started nothing"
        )
    return {
        "project": project,
        "unit": watch_unit_name(project),
        "unit_path": str(path),
        "unit_changed": bool(changed),
        "service_active": was_active or start_required,
        "started": start_required,
        "detail": detail,
        "environment": environment,
        "lingering": lingering,
        "registration": registration,
        "watcher_live": watch_state(project)["watcher_live"],
        # Which path armed the watcher, as a field rather than prose: a caller
        # acts on a value instead of parsing a sentence out of ``detail``.
        "path": "service",
        "fallback_reason": None,
        "ensure_line": watcher_ensure_line(project),
    }


# The two reasons a service arming is abandoned for a plain process. Named
# rather than inlined so the sentence a coordinator reads distinguishes them,
# and so a caller deciding whether a fallback is expected can match on the
# value instead of on a phrase, which drifts.
_SERVICE_UNREACHABLE_CAUSE = "the service manager could not be reached"
_LINGER_FALLBACK_CAUSE = (
    "lingering could not be enabled, so a unit would stop at this account's logout"
)


def _watcher_armed_as_process(
    project: str,
    *,
    error: BaseException,
    unit_path: Path,
    unit_changed: bool,
    environment: Mapping[str, str],
    producer: Callable[[str], Mapping[str, Any]],
    cause: str = _SERVICE_UNREACHABLE_CAUSE,
) -> dict[str, Any]:
    """Arm the watcher as a plain process after the service path was refused.

    The result keeps the shape the service path returns, so a caller reads one
    result either way, and states the fallback in three fields: ``path``, the
    failure that forced it, and whether a watcher is live afterwards. The
    producer reports liveness rather than raising, so a fallback that could not
    start a watcher reads as ``watcher_live`` false with the reason beside it,
    which is the same shape the dispatch path reports.

    ``cause`` names which failure forced the fallback, because two do: a manager
    that cannot be reached, and one that will not keep this account's units past
    logout. The error text alone does not say which, and the sentence a coordinator
    reads has to.
    """
    state = producer(project)
    registration = _register_watch_unit(project, watch_unit_name(project))
    live = bool(state.get("watcher_live"))
    reason = " ".join(str(error).split())
    return {
        "project": project,
        "unit": watch_unit_name(project),
        "unit_path": str(unit_path),
        "unit_changed": bool(unit_changed),
        "service_active": False,
        "started": live,
        "detail": (
            f"{cause} ({reason}); "
            + (
                f"armed the watcher for {project} as a plain background process"
                if live
                else f"a plain background watcher for {project} is not live either"
            )
        ),
        "environment": environment,
        "lingering": None,
        "registration": registration,
        "watcher_live": live,
        "path": "fallback",
        "fallback_reason": reason,
        "ensure_line": watcher_ensure_line(project),
    }


# Every state the watch surface can emit, split by whether a coordinator has to
# act on it. The first set is the vocabulary a reader acts on the sight of; the
# second is progress and is not news on its own. The split used to feed a
# follower state filter, which is gone — a follower now delivers every
# transition — but the vocabulary remains the one the surface knows, and tests
# still assert every emitted state lands in it.
WATCH_ATTENTION_STATES = (
    "complete",
    "blocked",
    "failed",
    "launch-failed",
    "stalled",
    "stopped",
    "abandoned",
    "lane-event",
    "completed_unpromoted",
    "unknown",
    "unreadable",
    "exited-unfinished",
    "wait-aged",
)
WATCH_PROGRESS_STATES = (
    "dispatched",
    "working",
    "running",
    "waiting",
    "promoted",
    "recorded",
)


def _watch_arming_line(project: str) -> str:
    """Return the exact shell-safe command a dispatch payload carries."""
    return f"reckon crew watch --project {shlex.quote(project)}"


# The unit exports this into the watcher's own environment, so the seat record
# a service-armed watcher claims names the unit that will replace it. Read from
# the environment rather than passed as an argument: the watcher's argv is the
# arming contract a person copies, and a path-only flag would appear there.
WATCH_UNIT_ENV = "RECKON_WATCH_UNIT"

# A watcher holds its seat for as long as it runs, so a service that dies at
# logout is the fault this deployment exists to remove: none of the units it
# owns come back, and the project reads as watched until the next dispatch
# refuses. Lingering is what keeps a user manager alive past the last session.
LINGER_IF_REQUIRED = True

# The arming path sets this to the file the watcher it starts must append its
# stdout and stderr to, and the watcher reads it when it takes its seat. Read
# from the environment for the same reason the unit name is: the watcher's argv
# is the arming contract a person copies, and a path-only flag would appear
# there. A watcher a service manager started leaves it unset, because systemd
# already appends the unit's output to that same file.
WATCH_LOG_ENV = "RECKON_WATCH_LOG"

# A watcher's own log is diagnostic, not a ledger, so it is bounded: the file is
# rotated to a single ``.1`` sibling once it passes this size, which keeps the
# most recent output a reader needs to date a producer's death without letting
# an unmanaged watcher fill the shared home over months.
WATCH_LOG_MAX_BYTES = 2 * 1024 * 1024


def ensure_placement_reservation(
    *,
    session: str | None = None,
    project: str | None = None,
    runner: Any | None = None,
    alive_probe: Any | None = None,
) -> dict[str, Any]:
    """Hold the placement reservation if absent, and report it if present.

    The reservation is a durable shared resource of the same kind as the model
    serve and the project watcher, so it is managed the same way: an ensure
    command that is safe to run twice, holding the resource once and reporting
    it afterwards. Its job id is published into the shared crew state rather
    than held in the session that ran the command, because a job id in one
    session's memory is invisible to every other session and each would hold
    its own reservation.
    """
    from reckon.crew import placement as placement_module

    result = placement_module.ensure_reservation(
        session=session, project=project, runner=runner, alive_probe=alive_probe
    )
    result["ensure_line"] = placement_ensure_line()
    return result


def placement_ensure_line() -> str:
    """Return the command that holds or reports the placement reservation."""
    return "reckon crew placement --ensure"


def watch_unit_name(project: str) -> str:
    """Return the systemd user unit that runs one project's watcher service."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", project).strip("-") or "project"
    return f"reckon-watch-{readable}.service"


def watch_log_path(project: str) -> Path:
    """Return the file a project's watcher appends its output to.

    One path serves both arming routes, so a reader who finds a dead seat finds
    that producer's last words whichever way it was started: a service manager
    appends the unit's stdout and stderr here, and an unmanaged watcher adopts
    the same file when it takes its seat.
    """
    return _config_home() / "logs" / f"watch-{watch_unit_name(project)}.log"


class _WatchLogStream:
    """A stdout/stderr stand-in that appends timestamped lines to a file.

    A watcher's log is read after the fact, so every line it carries has to say
    when it arrived — otherwise a traceback says what killed the producer but
    not whether that was an hour or a week ago. The file is rotated to a single
    ``.1`` sibling once it passes :data:`WATCH_LOG_MAX_BYTES`, so a watcher left
    running for months cannot fill the shared home.

    Accepts bytes as well as text because a library writing to ``sys.stdout``
    may reach for a binary writer once it sees a non-tty stream, and silently
    dropping those writes would lose exactly the diagnostics this exists for.
    """

    def __init__(self, path: Path, *, max_bytes: int = WATCH_LOG_MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._handle = None
        # The descriptors this stream owns once it is adopted. They are kept
        # because rotation replaces the file: a descriptor pointed at the log
        # once keeps writing the rotated-away inode, so a library's unbuffered
        # write or the interpreter's own traceback would be lost from the log a
        # reader opens.
        self._descriptors: tuple[int, ...] = ()
        self._at_line_start = True
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            oversized = self.path.stat().st_size >= self.max_bytes
        except FileNotFoundError:
            oversized = False
        if oversized:
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._handle = self.path.open("a", encoding="utf-8", errors="replace")

    def adopt_descriptors(self, descriptors: Iterable[int]) -> None:
        """Send these process descriptors to this stream's file, and keep them.

        Called by :func:`_adopt_watch_log` once, when the watcher takes its seat.
        Keeping them is what makes the redirection survive rotation: the file a
        descriptor was pointed at is renamed away when the log rotates, so a
        descriptor pointed once and never re-pointed writes an inode nothing
        reads.
        """
        self._descriptors = tuple(descriptors)
        self._point_descriptors()

    def _point_descriptors(self) -> None:
        source = self._handle.fileno()
        for target in self._descriptors:
            try:
                os.dup2(source, target)
            except OSError:
                continue

    def _rotate_if_needed(self) -> None:
        try:
            size = self._handle.tell()
        except (OSError, ValueError):
            return
        if size < self.max_bytes:
            return
        self._handle.close()
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._handle = self.path.open("a", encoding="utf-8", errors="replace")
        # Re-point every adopted descriptor at the new file. Without this the
        # descriptors keep the rotated-away inode, so a traceback or an
        # unbuffered library write after the first rotation never reaches the
        # log a reader opens.
        self._point_descriptors()

    def write(self, text: Any) -> int:
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", "replace")
        pieces: list[str] = []
        for line in str(text).splitlines(keepends=True):
            if self._at_line_start and line.strip():
                pieces.append(f"[{_utc_now()}] ")
            pieces.append(line)
            self._at_line_start = line.endswith("\n")
        self._rotate_if_needed()
        self._handle.write("".join(pieces))
        self._handle.flush()
        return len(text)

    def flush(self) -> None:
        self._handle.flush()

    def fileno(self) -> int:
        return self._handle.fileno()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        self._handle.close()


def _adopt_watch_log() -> Path | None:
    """Send this process's stdout and stderr to the log its arming named.

    Called by the watcher when it takes its seat, which is the point at which a
    plain background process becomes the project's producer. Returns the path it
    adopted, or ``None`` when the arming named none — a service-armed watcher's
    output is already appended to that file by its unit, so it has nothing to
    adopt.

    The environment entry is removed on adoption so a watcher that starts
    anything of its own cannot hand the same log on to it. Both the file
    descriptor and the Python streams are redirected: the descriptors so a
    library writing unbuffered output, or the interpreter printing an uncaught
    traceback, still lands in the log, and the Python streams so each line
    carries the time it was written.
    """
    target = os.environ.pop(WATCH_LOG_ENV, "")
    if not target:
        return None
    log = _WatchLogStream(Path(target))
    sys.stdout = log
    sys.stderr = log
    log.adopt_descriptors((1, 2))
    return log.path


def watcher_ensure_line(project: str) -> str:
    """Return the command that starts or restarts a project's watcher service."""
    return f"reckon crew watch --ensure --project {shlex.quote(project)}"


def watch_cycle_line(project: str) -> str:
    """Return the command pair that takes a stale watcher onto current code.

    The ensure command alone cannot do it: a unit already active on an unchanged
    definition is reported rather than restarted, and a watcher armed as a plain
    process is not a unit at all. Releasing the seat first is what makes the
    arming take — ``unwatch`` stops the registered watcher and clears its
    record, so the arming that follows finds no live seat to contend with and
    starts on the code on disk now.
    """
    return (
        f"reckon crew unwatch --project {shlex.quote(project)} && "
        + watcher_ensure_line(project)
    )


def _watch_attach_line(project: str, *, session: str | None = None) -> str:
    """Return the follower one session arms to be woken about its own runs.

    A seat existing is not the same as this session hearing about it: the seat
    is project-global and wake delivery is session-local, so a caller
    dispatching against another session's seat is told a watcher is live while
    nothing reaches it. This is the command that closes that gap.

    The command's first token is the absolute path of the running reckon
    console script, because the shell that arms it need not carry the
    interpreter's bin directory on PATH.

    It is one bare command on purpose: filtering and buffering belong inside
    the follower, because a shell pipeline around it has three ways to swallow
    the ticker silently. An unbuffered stage withholds every line until the
    command exits, and this command does not exit. An unanchored pattern
    matches the summary field that trails each line, so it matches everything.
    And a trailing ``|| true`` turns the follower's own refusal into a silent
    success indistinguishable from a quiet fleet.

    It carries no state filter either. A filter that legitimately matches
    nothing produces an empty pane, which reads the same as a follower that
    never started -- and a reader watching a wave wants the starts and the
    working transitions, not only the landings. A state filter is worse than
    no filter even when it matches: it reports how a run stopped and hides how
    it recovered, which is the half of the story the reader is waiting for.
    """
    parts = [
        shlex.quote(_reckon_console_script()),
        "crew",
        "follow",
        "--project",
        shlex.quote(project),
    ]
    if session:
        parts += ["--session", shlex.quote(session)]
    return " ".join(parts)


from .follower_registration import (  # noqa: E402
    follower_state,
)


from .process_liveness import (  # noqa: E402
    process_alive,
    record_process_alive,
)


from .run_paths import (  # noqa: E402
    _read_watch_record,
    _utc_now,
    _write_watch_record,
    watch_lock_path,
    watch_stream_path,
)
