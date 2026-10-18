"""The served process records each request's duration and bounds its log.

Every latency the site reports was reconstructed by timing requests by hand,
because the access log carried no duration; each answered request now logs one
line with its duration in milliseconds and answers with the same figure in a
``Server-Timing`` header. The served log, which the systemd unit opens with
``StandardOutput=append:``, is bounded: once it passes its bound its content is
moved to a single predecessor and the live file is emptied in place.
"""

from __future__ import annotations

import re
import socket
import threading
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


def test_one_request_logs_its_duration_and_sends_a_timing_header(
    served, capsys
) -> None:
    status, headers, _ = served.get(_SERVED_PATH)
    assert status == 200

    header = headers.get("Server-Timing")
    assert header is not None, "the response carried no Server-Timing header"
    match = _TIMING_HEADER.match(header)
    assert match, f"Server-Timing carried no duration: {header!r}"
    header_ms = float(match.group(1))
    assert header_ms >= 0.0

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if f"GET {_SERVED_PATH}" in line]
    assert lines, f"no access line was logged for the request:\n{out}"
    on_line = _DURATION_ON_LINE.search(lines[-1])
    assert on_line, f"the access line carried no duration: {lines[-1]!r}"
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
