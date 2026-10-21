"""A served log keeps the home its server first served under.

The served log's path was resolved again for every access line and again before
every request, each time through the configuration home the environment names.
A change stream's handler runs on a thread that outlives the request that opened
it, so its closing access line is written after a test's teardown has already
restored ``RECKON_HOME``; that line then resolved to the operator's own
``~/.config/reckon/logs/server.log`` rather than the test's home, and a request
that found the bound crossed could rotate the operator's log the same way. A
server now resolves its log path once, at its first request, and keeps it on
itself, so a home that moves out from under a live server cannot retarget a
later line or a rotation.

The two controls below are the deterministic proof that the defect is real: with
the access line's path resolved per line again the closing line lands in the
moved-to home, and with the bound check's path resolved per call again the first
home's log is never rotated. Neither case names a real home: the second home is
a temporary directory standing in for the operator's.
"""

from __future__ import annotations

import contextlib
import select
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from reckon import serve

PROJECT = "sample"

#: A path the plain handler answers from the configuration home itself, so a
#: case needs no docs tree to bind a server to its log path.
_BINDING_PATH = "/_projects/mounts.json"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate, *, timeout: float = 10.0) -> bool:
    """Poll ``predicate`` until it holds or the bound elapses."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _home(tmp_path: Path, name: str) -> Path:
    """A configuration home whose log directory is already present."""

    home = tmp_path / name
    (home / "logs").mkdir(parents=True)
    (home / "mounts.json").write_text("{}\n", encoding="utf-8")
    return home


def _log_file(home: Path) -> Path:
    return home / "logs" / "server.log"


def _lines(home: Path) -> list[str]:
    try:
        text = _log_file(home).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()


def _lines_with(home: Path, marker: str) -> list[str]:
    return [line for line in _lines(home) if marker in line]


class _QuietWatch:
    """A per-tree watch that reports nothing and ends with its client.

    The stream's own loop is not what these cases measure: the point is where a
    handler's access line lands, so the filesystem side is stubbed to a wait
    that returns as soon as the client disconnects and the handler unwinds.
    """

    def __init__(self, root: Path, *, wake_fd: int | None = None) -> None:
        self.root = Path(root).resolve()
        self._wake_fd = wake_fd

    def wait(self, connection: socket.socket) -> bool:
        select.select([connection], [], [])
        return False

    def drain(self, settle_s: float) -> None:
        return None

    def close(self) -> None:
        return None


class _ChangeStream:
    """Read ``/_changes/<project>`` frames over a raw socket.

    A raw socket rather than ``http.client`` because closing it has to deliver
    an end-of-file the handler can select on, which is what makes the handler
    write its closing access line while the case still holds the process.
    """

    def __init__(self, port: int, project: str) -> None:
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self._sock.sendall(
            f"GET /_changes/{project} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
        )
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise AssertionError("the change stream closed during its headers")
            buffer += chunk

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        self._sock.close()


class _Served:
    """A bare server on its own thread, over a temporary configuration home."""

    def __init__(self, port: int, server, thread: threading.Thread, home: Path) -> None:
        self.port = port
        self.home = home
        self._server = server
        self._thread = thread

    def get(self, path: str) -> int:
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(10)


@pytest.fixture()
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A bare ``ThreadingHTTPServer`` around ``serve.Handler``, as tests build."""

    home = _home(tmp_path, "home")
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", None)
    monkeypatch.setattr(serve, "load_mounts", lambda: {PROJECT: docs})
    monkeypatch.setattr(serve, "_ProjectChangeWatch", _QuietWatch)

    port = _free_port()
    server = serve.ThreadingHTTPServer(("127.0.0.1", port), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    harness = _Served(port, server, thread, home)
    try:
        yield harness
    finally:
        harness.stop()


def test_a_stream_closing_after_the_home_moves_logs_to_the_first_home(
    served: _Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live server's closing line lands in the home it first served under.

    Control: resolve the access line's path per line again and the moved-to
    home gains the line, so the assertion that it holds no log fails.
    """

    first = served.home
    second = _home(tmp_path, "operator")

    # The binding request: the server's first, so it resolves and keeps the
    # first home's log path here, before anything moves.
    assert served.get(_BINDING_PATH) == 200
    # The access line is written by the handler thread, which may reach its
    # finally-block just after the response has already reached the client, so
    # the read is polled rather than taken once.
    assert _wait_for(lambda: bool(_lines_with(first, _BINDING_PATH))), (
        "the binding request never logged its access line, so this case could "
        "not tell a kept path from a moved one"
    )

    stream = _ChangeStream(served.port, PROJECT)
    try:
        monkeypatch.setenv("RECKON_HOME", str(second))
    finally:
        # The handler thread unwinds here and writes its closing line after the
        # move, which is exactly the window the defect needs.
        stream.close()

    assert _wait_for(lambda: bool(_lines_with(first, f"/_changes/{PROJECT}"))), (
        "the stream's closing line never reached the home the server started "
        f"under; first home log:\n{chr(10).join(_lines(first))}"
    )
    assert not _log_file(second).exists(), (
        "the closing line followed the moved home; the operator's log would "
        f"have gained it:\n{chr(10).join(_lines(second))}"
    )


def test_a_request_after_the_home_moves_rotates_the_first_home_log(
    served: _Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request after the move rotates the log of the home it first served.

    Control: resolve the bound check's path per call again and it reads the
    moved-to home, finds no log there, and leaves the first home's log
    unrotated, so the assertion that a predecessor appeared fails.
    """

    first = served.home
    second = _home(tmp_path, "operator")

    # The binding request runs its own bound check first, so the over-bound log
    # is arranged after it: a log pushed past the bound before the binding
    # request would be rotated by that request instead of by the one below.
    assert served.get(_BINDING_PATH) == 200
    first_log = _log_file(first)
    monkeypatch.setattr(serve, "SERVER_LOG_BOUND_BYTES", 1024)
    first_log.write_bytes(b"a line that predates the move\n" * 128)
    assert first_log.stat().st_size > 1024

    monkeypatch.setenv("RECKON_HOME", str(second))
    # One request after the move: its bound check must rotate the first home's
    # log. The response is irrelevant; this case measures the rotation only.
    served.get(_BINDING_PATH)

    predecessor = first_log.with_name(first_log.name + ".1")
    assert _wait_for(lambda: predecessor.exists() and predecessor.stat().st_size > 0), (
        "the first home's over-bound log was never rotated after the move; "
        f"predecessor present: {predecessor.exists()}"
    )
    assert not _log_file(second).exists(), (
        "the rotation followed the moved home; the operator's log would have "
        f"been created:\n{chr(10).join(_lines(second))}"
    )
