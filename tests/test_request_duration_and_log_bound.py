"""The served process records each request's duration and bounds its log.

Every latency the site reports was reconstructed by timing requests by hand,
because the access log carried no duration; each answered request now logs one
line with its duration in milliseconds and answers with the same figure in a
``Server-Timing`` header. The served log, which the systemd unit opens with
``StandardOutput=append:``, is bounded: once it passes its bound its content is
moved to a single predecessor and the live file is emptied in place. The empty
and every access line take one lock, so a line written while the log is being
rotated is never dropped between the copy and the empty.
"""

from __future__ import annotations

import re
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from reckon import serve
from reckon.service import log_path

_TIMING_HEADER = re.compile(r"^app;dur=(\d+(?:\.\d+)?)$")
_DURATION_ON_LINE = re.compile(r"(\d+(?:\.\d+)?)ms\b")

# A path the plain handler answers without any project mounted: it serves the
# configuration home's own files, so the cases need no docs tree.
_SERVED_PATH = "/_projects/mounts.json"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate, *, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it holds or the bound elapses.

    The access line is written by the handler thread, which may reach its
    finally-block just after the response has already reached the client, so a
    read taken immediately after the response is a race.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Served:
    """A served process on its own thread, over a temporary configuration home."""

    def __init__(self, port: int, server, thread: threading.Thread, home: Path) -> None:
        self.port = port
        self.home = home
        self._server = server
        self._thread = thread

    def get(self, path: str) -> tuple[int, dict, bytes]:
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status), dict(response.headers), response.read()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(10)


@pytest.fixture()
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The per-test configuration home is where the unit's log lives; the
    # isolation fixture already points RECKON_HOME at a temporary home, and this
    # narrows it to this case's own tree so the log path is known exactly.
    home = tmp_path / "config"
    (home / "logs").mkdir(parents=True)
    (home / "mounts.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", None)

    port = _free_port()
    server = serve.ThreadingHTTPServer(("127.0.0.1", port), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    harness = _Served(port, server, thread, home)
    try:
        yield harness
    finally:
        harness.stop()


def _access_lines(path: Path, marker: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if marker in line]


def test_a_line_written_during_rotation_survives(served) -> None:
    """A line written while the log is being rotated survives the rotation.

    The rotator copies the live file and then empties it. A line written between
    those two steps is in neither the predecessor nor the emptied file, so it is
    lost; this drives exactly that interleaving deterministically by holding the
    rotator inside its copy, starting the write there, and letting the rotation
    finish. The line must then be in the live file — written after the empty,
    because the writer waits on the same lock the rotator holds.
    """

    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"a logged access line\n" * 64)

    copied = threading.Event()
    release = threading.Event()
    real_copyfileobj = serve.shutil.copyfileobj

    def slow_copyfileobj(src, dst, *args, **kwargs):
        result = real_copyfileobj(src, dst, *args, **kwargs)
        copied.set()
        release.wait(5)
        return result

    serve.shutil.copyfileobj = slow_copyfileobj
    try:
        rotated: list[bool] = []
        rotator = threading.Thread(
            target=lambda: rotated.append(serve._bound_server_log(log, bound=1)),
            daemon=True,
        )
        rotator.start()
        assert copied.wait(5), "the rotator never reached its copy"

        writer = threading.Thread(
            target=lambda: serve._write_access_line("SENTINEL rotation probe", log),
            daemon=True,
        )
        writer.start()
        writer.join(0.3)
        release.set()
        rotator.join(5)
        writer.join(5)
    finally:
        serve.shutil.copyfileobj = real_copyfileobj

    assert rotated == [True], "the over-bound log was not rotated"
    live = log.read_text(encoding="utf-8", errors="replace")
    assert "SENTINEL rotation probe" in live, (
        "a line written while the log was rotating was dropped by the empty; "
        f"the live file holds {live!r}"
    )


def test_one_request_logs_its_duration_and_sends_a_timing_header(served) -> None:
    status, headers, _ = served.get(_SERVED_PATH)
    assert status == 200

    header = headers.get("Server-Timing")
    assert header is not None, "the response carried no Server-Timing header"
    match = _TIMING_HEADER.match(header)
    assert match, f"Server-Timing carried no duration: {header!r}"
    header_ms = float(match.group(1))
    assert header_ms >= 0.0

    log = log_path()
    assert _wait_for(lambda: _access_lines(log, f"GET {_SERVED_PATH}")), (
        f"no access line for the request reached the served log {log}"
    )
    on_line = _DURATION_ON_LINE.search(_access_lines(log, f"GET {_SERVED_PATH}")[-1])
    assert on_line, "the access line carried no duration"
    assert float(on_line.group(1)) >= 0.0


def test_the_log_is_bounded_and_keeps_one_predecessor(served, monkeypatch) -> None:
    bound = 4096
    monkeypatch.setattr(serve, "SERVER_LOG_BOUND_BYTES", bound)
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    original = b"a logged access line\n" * ((bound // 20) + 64)
    assert len(original) > bound, "the fixture did not drive the log past its bound"
    log.write_bytes(original)

    served.get(_SERVED_PATH)

    predecessor = log.with_name(log.name + ".1")
    assert predecessor.is_file(), "the over-bound log was not rotated to a predecessor"
    assert predecessor.read_bytes() == original, (
        "the predecessor does not hold the log content from before the rotation"
    )
    assert log.stat().st_size <= bound, (
        "the live log was not bounded after the rotation"
    )


if __name__ == "__main__":  # pragma: no cover - manual launch
    raise SystemExit(pytest.main([__file__]))
