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
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "reckon.service"

UNIT_TEMPLATE = """\
[Unit]
Description=reckon plan server
Documentation=https://github.com/Simon-McIntosh/reckon
After=network.target

[Service]
Type=simple
WorkingDirectory={working_directory}
Environment="PATH={path}"
{environment}\
ExecStart={exec_start}
StandardOutput=append:{log_file}
StandardError=append:{log_file}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""


class ServiceError(RuntimeError):
    """A systemd operation could not be completed."""


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


def unit_path() -> Path:
    """Return the path the reckon user unit is written to."""
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


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
    sibling = Path(sys.executable).resolve().parent / "reckon"
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

    return UNIT_TEMPLATE.format(
        working_directory=Path.home(),
        path=search_path,
        environment=environment,
        exec_start=" ".join(argv),
        log_file=log_path(),
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
        raise ServiceError(
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
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # systemd opens the log file but will not create its parent directory.
    log_path().parent.mkdir(parents=True, exist_ok=True)
    content = render_unit(port=port, host=host, mounts_file=mounts_file)
    unchanged = target.is_file() and target.read_text() == content
    if not unchanged:
        target.write_text(content)
    return target, not unchanged
