"""One MCP server per node, shared by every worker whose MCP set names it.

A stdio MCP server is one process per worker, so a node running many workers
pays one server process and one resident footprint per worker. A server
declared here runs once per node and every worker on that node reaches it over
the network transport instead.

The node's share lives in a node-local runtime directory holding, per server,
a lock file, a JSON record naming the live process and its endpoint, and the
server's log. A serving process holds the lock for its whole lifetime; a second
launch on the same node finds the lock taken and returns the published endpoint
rather than starting a second copy. The record is created privately and
replaced atomically, so a reader never sees a half-written record.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NODE_DIR_ENV = "RECKON_MCP_NODE_DIR"
TRANSPORT_ENV = "RECKON_MCP_TRANSPORT"
HOST_ENV = "RECKON_MCP_HOST"
PORT_ENV = "RECKON_MCP_PORT"
NAME_ENV = "RECKON_MCP_NAME"

STREAMABLE_PATH = "/mcp"
DEFAULT_NAME = "reckon"
DEFAULT_HOST = "127.0.0.1"


class SharedServerError(RuntimeError):
    """A node's shared server could not be brought up."""


@dataclass(frozen=True)
class SharedServer:
    """A live server on this node: where it listens and which process serves it."""

    name: str
    pid: int
    host: str
    port: int
    url: str
    started_at: str
    command: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "started_at": self.started_at,
            "command": list(self.command),
        }


def node_dir() -> Path:
    """Return this node's runtime directory, creating it privately if absent."""
    override = os.environ.get(NODE_DIR_ENV)
    if override:
        root = Path(override)
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        root = Path(runtime) / "reckon-mcp"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(root)


def record_path(name: str, directory: Path | None = None) -> Path:
    return (directory or node_dir()) / f"{name}.json"


def lock_path(name: str, directory: Path | None = None) -> Path:
    return (directory or node_dir()) / f"{name}.lock"


def log_path(name: str, directory: Path | None = None) -> Path:
    return (directory or node_dir()) / f"{name}.log"


def endpoint_url(host: str, port: int) -> str:
    return f"http://{host}:{port}{STREAMABLE_PATH}"


def read_record(name: str, directory: Path | None = None) -> SharedServer | None:
    path = record_path(name, directory)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    try:
        return SharedServer(
            name=str(data["name"]),
            pid=int(data["pid"]),
            host=str(data["host"]),
            port=int(data["port"]),
            url=str(data["url"]),
            started_at=str(data["started_at"]),
            command=tuple(str(part) for part in data.get("command") or ()),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _port_accepts(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def live_record(
    name: str, directory: Path | None = None, *, probe: bool = True
) -> SharedServer | None:
    """Return the record of a server actually serving, or ``None``.

    A record whose process has exited is not a server, so the stale file is
    removed and the next launch is free to start one.
    """
    record = read_record(name, directory)
    if record is None:
        return None
    if not _pid_alive(record.pid):
        with suppress(OSError):
            record_path(name, directory).unlink()
        return None
    if probe and not _port_accepts(record.host, record.port):
        return None
    return record


def default_command() -> list[str]:
    """The argv that starts a shared server under this interpreter."""
    return [sys.executable, "-m", "reckon.mcp"]


def _write_record(record: SharedServer, directory: Path) -> None:
    path = record_path(record.name, directory)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record.as_dict(), stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _await_record(
    name: str, directory: Path, timeout: float = 20.0
) -> SharedServer | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = live_record(name, directory)
        if record is not None:
            return record
        time.sleep(0.05)
    return None


def ensure_shared_server(
    name: str = DEFAULT_NAME,
    *,
    command: Sequence[str] | None = None,
    directory: Path | None = None,
    start: bool = True,
    timeout: float = 20.0,
) -> SharedServer | None:
    """Return this node's server for ``name``, starting it when absent.

    The check-then-start race is resolved by the server itself: whichever
    process takes the lock serves, and every other launcher reads the record it
    publishes. A caller that cannot wait gets ``None``, so a launch can fall
    back to the harness's own declaration rather than losing the server.
    """
    directory = directory if directory is not None else node_dir()
    existing = live_record(name, directory)
    if existing is not None:
        return existing
    if not start:
        return None
    argv = list(command) if command else default_command()
    env = dict(os.environ)
    env[NODE_DIR_ENV] = str(directory)
    env[NAME_ENV] = name
    env.setdefault(TRANSPORT_ENV, "streamable-http")
    log = log_path(name, directory)
    with open(log, "ab") as stream:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    return _await_record(name, directory, timeout=timeout)


def serve(
    name: str,
    runner: Callable[[str, int], None],
    *,
    host: str = DEFAULT_HOST,
    port: str | int = 0,
    directory: Path | None = None,
    command: Sequence[str] | None = None,
) -> SharedServer:
    """Hold this node's single instance for ``name`` and serve until it ends.

    The lock is held for the process's whole lifetime, so a second server for
    the same name cannot bind a second endpoint. When the lock is already held
    the existing server's record is returned instead of a second copy being
    started, which is what makes a launch's ensure call idempotent however many
    workers make it at once.
    """
    directory = directory if directory is not None else node_dir()
    lock = lock_path(name, directory)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            existing = _await_record(name, directory)
            if existing is None:
                raise SharedServerError(
                    f"a server named {name!r} holds this node's lock but "
                    "published no endpoint"
                ) from exc
            return existing
        bound_port = int(port) or _free_port(host)
        record = SharedServer(
            name=name,
            pid=os.getpid(),
            host=host,
            port=bound_port,
            url=endpoint_url(host, bound_port),
            started_at=datetime.now(UTC).isoformat(),
            command=tuple(command or ()),
        )
        _write_record(record, directory)
        try:
            runner(host, bound_port)
        finally:
            with suppress(OSError):
                record_path(name, directory).unlink()
        return record
    finally:
        os.close(descriptor)


def worker_server_config(record: SharedServer) -> dict[str, Any]:
    """The harness declaration that points a worker at the node's server."""
    return {"type": "http", "url": record.url}


def shared_server_urls(
    names: Sequence[str],
    declarations: Mapping[str, Any] | None,
    *,
    start: bool = True,
    directory: Path | None = None,
    timeout: float = 20.0,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return endpoints for the named shared servers, and the failures.

    A name the flight does not declare as shared is left alone, so a node whose
    set names only stdio servers is unchanged. A shared declaration that cannot
    be brought up is reported rather than refusing the launch: the worker keeps
    the declaration its set already resolved and the run record shows which
    server fell back.
    """
    declarations = declarations or {}
    endpoints: dict[str, str] = {}
    failures: dict[str, str] = {}
    for name in names:
        declaration = declarations.get(name)
        if declaration is None:
            continue
        command = None
        if isinstance(declaration, Mapping):
            declared_command = declaration.get("command")
            if declared_command is not None:
                command = [str(part) for part in declared_command]
        try:
            record = ensure_shared_server(
                name,
                command=command,
                start=start,
                directory=directory,
                timeout=timeout,
            )
        except OSError as exc:
            failures[name] = str(exc)
            continue
        if record is None:
            failures[name] = "no endpoint was published"
        else:
            endpoints[name] = record.url
    return endpoints, failures
