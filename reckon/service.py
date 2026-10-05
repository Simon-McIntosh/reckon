"""Manage the reckon HTTP server as a systemd user service.

The server is a long-lived daemon: it must come back by itself after a crash,
survive logout, and write its output somewhere durable. A terminal multiplexer
provides none of that, so the supported deployment is a systemd *user* unit.

Two host properties decide whether this works, and both are checked here rather
than assumed:

- the per-user systemd manager must be running (``systemctl --user``);
- lingering must be enabled, otherwise the manager — and every unit it owns —
  is torn down when the last login session ends.
"""

import os
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

UNIT_NAME = "reckon.service"

#: The environment variable naming the user's config root, per the XDG base
#: directory specification. A consumer that resolves a directory under it must
#: read the same variable the application reading that directory would.
XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"

UNIT_TEMPLATE = """\
[Unit]
Description=reckon plan server
Documentation=https://github.com/Simon-McIntosh/reckon
After=network.target
ConditionHost={host_name}

[Service]
Type=simple
WorkingDirectory={working_directory}
Environment="PATH={path}"
{environment}\
ExecStart={exec_start}
StandardOutput=append:{log_file}
StandardError=append:{log_file}
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""


class ServiceError(RuntimeError):
    """A systemd operation could not be completed."""


class LingerUnavailableError(ServiceError):
    """Lingering could not be enabled, so a unit would stop at logout.

    Distinct from a plain :class:`ServiceError` because a caller can act on it
    rather than only report it: the manager is reachable and the unit is fine,
    but this account's units will not outlive its login session, so an arming
    path that must leave a watcher behind has to place it outside the manager.
    Measured 2026-09-25 on a fleet compute node, where ``loginctl`` refused with
    ``Could not enable linger: No such device or address``.
    """


def node_executable() -> Path:
    """Locate the Node.js interpreter used for server-side JSX compilation."""
    configured = os.environ.get("RECKON_NODE")
    candidates = [Path(configured).expanduser()] if configured else []
    discovered = shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        [
            Path.home() / ".local" / "bin" / "node",
            Path.home() / ".hermes" / "node" / "bin" / "node",
            Path("/usr/local/bin/node"),
            Path("/usr/bin/node"),
            Path("/bin/node"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise ServiceError(
        "cannot locate the Node.js interpreter required for JSX compilation; "
        "install node or set RECKON_NODE to its executable"
    )


def xdg_config_home(environ: Mapping[str, str] | None = None) -> Path:
    """The user's XDG configuration base directory.

    Resolved the way XDG-aware tools resolve it: ``XDG_CONFIG_HOME`` when the
    environment names one, otherwise ``~/.config``. Consumers append the
    subdirectory each one reads, so the base is resolved here once however many
    of them build on it.
    """
    environ = os.environ if environ is None else environ
    base = environ.get(XDG_CONFIG_HOME_ENV)
    return Path(base).expanduser() if base else Path.home() / ".config"


def systemd_user_dir() -> Path:
    """The directory the user's systemd units are read from.

    Resolved the way systemd itself resolves it: ``XDG_CONFIG_HOME`` when the
    user set one, otherwise ``~/.config``. The installer writes here so the
    units land where the manager looks for them, and a caller that isolates
    ``XDG_CONFIG_HOME`` runs against its own directory rather than the
    operator's -- which is what lets a test exercise the install without ever
    reaching the real user manager.
    """
    return xdg_config_home() / "systemd" / "user"


def unit_path(unit_name: str = UNIT_NAME, *, directory: Path | None = None) -> Path:
    """Return a named user unit's path in the manager's unit directory."""
    return (directory if directory is not None else systemd_user_dir()) / unit_name


def log_path() -> Path:
    """Return the file the service appends its output to.

    Output goes to a plain file rather than the journal because reading a user
    journal requires membership of a privileged group that a plain account on a
    managed host does not have — a service whose logs nobody can read is the
    failure mode this deployment exists to remove.
    """
    from reckon._store import _config_home

    return _config_home() / "logs" / "server.log"


def server_executable() -> Path:
    """Locate the ``reckon`` console script backing the running interpreter.

    The unit runs without a shell, so ExecStart needs an absolute path. The
    interpreter's own bin directory is preferred because it pins the unit to
    the environment the command was invoked from; PATH is only a fallback.
    """
    # A virtualenv's python is often a symlink to a shared interpreter, and the
    # console script sits beside the symlink, so look there before following it.
    interpreter = Path(sys.executable)
    for bin_dir in (interpreter.parent, interpreter.resolve().parent):
        sibling = bin_dir / "reckon"
        if sibling.is_file():
            return sibling
    discovered = shutil.which("reckon")
    if discovered:
        return Path(discovered).resolve()
    raise ServiceError(
        "cannot locate the 'reckon' console script; install the package into "
        "the environment you are invoking it from"
    )


def render_unit(
    port: int = 8765,
    host: str | None = None,
    mounts_file: Path | None = None,
    executable: Path | None = None,
    node: Path | None = None,
) -> str:
    """Render the systemd unit that runs ``reckon serve``."""
    command = executable or server_executable()
    argv = [str(command), "serve", "--port", str(port)]
    if host:
        argv += ["--host", host]
    if mounts_file:
        argv += ["--mounts", str(Path(mounts_file).expanduser().resolve())]

    bin_dir = str(Path(command).parent)
    node_bin_dir = str((node or node_executable()).parent)
    search_path = os.pathsep.join(
        dict.fromkeys([bin_dir, node_bin_dir, "/usr/local/bin", "/usr/bin", "/bin"])
    )

    # Forward an explicit config home so the unit resolves the same mounts.json
    # and state root as the shell that installed it.
    environment = ""
    config_override = os.environ.get("RECKON_HOME")
    if config_override:
        resolved = Path(config_override).expanduser().resolve()
        environment = f'Environment="RECKON_HOME={resolved}"\n'

    # The unit file sits under the home directory, which every cluster node
    # mounts. A user manager starting on any other node would read the same
    # enabled unit and start a second server writing to the same log, so the
    # unit runs only on the host that installed it.
    return UNIT_TEMPLATE.format(
        host_name=socket.gethostname(),
        working_directory=Path.home(),
        path=search_path,
        environment=environment,
        exec_start=" ".join(argv),
        log_file=log_path(),
    )


def render_watch_unit(
    project: str,
    *,
    template: str,
    unit_name: str,
    unit_variable: str,
    log_file: Path,
    environment: Mapping[str, str],
    executable: str,
) -> str:
    """Render a project's watcher under its named user unit."""
    argv = [executable, "crew", "watch", "--project", project]
    override = "".join(
        f'Environment="{name}={value}"\n'
        for name, value in environment.items()
        if name != "PATH"
    )
    return template.format(
        project=project,
        working_directory=Path.home(),
        path=environment.get("PATH") or os.defpath,
        unit_variable=unit_variable,
        unit=unit_name,
        environment=override,
        exec_start=" ".join(shlex.quote(part) for part in argv),
        log_file=log_file,
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise ServiceError(f"{argv[0]} is not available on this host") from error


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``systemctl --user`` with the given arguments."""
    completed = _run(["systemctl", "--user", *args])
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ServiceError(f"systemctl --user {' '.join(args)} failed: {detail}")
    return completed


def user_manager_running() -> bool:
    """Report whether the per-user systemd manager is reachable."""
    completed = _run(["systemctl", "--user", "is-system-running"])
    # 'degraded' and 'starting' are still usable managers; only a hard failure
    # to reach the bus means units cannot be managed.
    return completed.returncode == 0 or (completed.stdout or "").strip() in {
        "degraded",
        "starting",
        "maintenance",
    }


def linger_enabled() -> bool:
    """Report whether the login manager keeps user units alive after logout."""
    completed = _run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"])
    return "Linger=yes" in (completed.stdout or "")


def enable_linger() -> None:
    """Ask the login manager to keep this user's units running after logout."""
    completed = _run(["loginctl", "enable-linger"])
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise LingerUnavailableError(
            f"could not enable lingering, so the service would stop at logout: {detail}"
        )


def installed() -> bool:
    """Report whether the reckon unit file exists."""
    return unit_path().is_file()


def require_installed() -> None:
    """Fail with an actionable message when the unit has not been written."""
    if not installed():
        raise ServiceError(
            f"{UNIT_NAME} is not installed; run 'reckon service install' first"
        )


def write_unit(
    port: int = 8765,
    host: str | None = None,
    mounts_file: Path | None = None,
) -> tuple[Path, bool]:
    """Write the unit file, returning its path and whether the content changed."""
    return write_named_unit(
        UNIT_NAME,
        render_unit(port=port, host=host, mounts_file=mounts_file),
        log_file=log_path(),
    )


def write_named_unit(
    unit_name: str,
    content: str,
    *,
    log_file: Path | None = None,
    directory: Path | None = None,
) -> tuple[Path, bool]:
    """Install a named user unit once per content change."""
    target = unit_path(unit_name, directory=directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    if log_file is not None:
        # systemd opens the log file but will not create its parent directory.
        log_file.parent.mkdir(parents=True, exist_ok=True)
    unchanged = target.is_file() and target.read_text(encoding="utf-8") == content
    if not unchanged:
        target.write_text(content, encoding="utf-8")
    return target, not unchanged


class SystemdUserUnitManager:
    """Control one family's named user units through the shared installer."""

    def __init__(
        self,
        unit_name: Callable[[str], str],
        log_file: Callable[[str], Path],
    ) -> None:
        self._unit_name = unit_name
        self._log_file = log_file

    def unit_path(self, project: str) -> Path:
        return unit_path(self._unit_name(project))

    def installed(self, project: str) -> bool:
        return self.unit_path(project).is_file()

    def active(self, project: str) -> bool:
        return (
            systemctl("is-active", self._unit_name(project), check=False).returncode
            == 0
        )

    def lingering(self) -> bool:
        return linger_enabled()

    def enable_linger(self) -> None:
        enable_linger()

    def write_unit(self, project: str, content: str) -> tuple[Path, bool]:
        return write_named_unit(
            self._unit_name(project), content, log_file=self._log_file(project)
        )

    def start(self, project: str, *, restart: bool) -> None:
        systemctl("daemon-reload")
        systemctl("restart" if restart else "start", self._unit_name(project))
