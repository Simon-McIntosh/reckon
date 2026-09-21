"""Origin checks use disposable launchers and loopback-only HTTP fixtures."""

import json
import socket
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from reckon.crew.serving_origin import check_serving_origin


@contextmanager
def origin_server(*, status=200, silent=False, redirect=None):
    requests = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if silent:
                release.wait(5)
                return
            self.send_response(status)
            if redirect:
                self.send_header("Location", redirect)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def launcher(tmp_path, origin, *, delay=0, payload=None, exit_status=0):
    path = tmp_path / "launcher"
    output = json.dumps({"origin": origin}) if payload is None else payload
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "assert sys.argv[1:] == ['--print-origin', '--model', 'synthetic-model']\n"
        f"time.sleep({delay!r})\n"
        f"print({output!r})\n"
        f"raise SystemExit({exit_status})\n"
    )
    path.chmod(0o700)
    return path


def check(path, *, timeout=5):
    return check_serving_origin(
        {"endpoints_document": "declaration-only.json"},
        path,
        origin_args=["--print-origin", "--model", "synthetic-model"],
        timeout=timeout,
    )


def test_answering_origin_reports_resolved_launcher_and_timeout(tmp_path, monkeypatch):
    # A proxy must not substitute a response from a different origin.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    with origin_server() as (origin, requests):
        target = launcher(tmp_path, origin)
        path = tmp_path / "linked-launcher"
        path.symlink_to(target)
        result = check(path)
    assert result["status"] == "serving"
    assert result["launcher_path"] == str(path)
    assert result["origin"] == origin
    assert result["answered"] is True
    assert result["http_status"] == 200
    assert result["timeout_seconds"] == 5
    assert result["timed_out"] is False
    assert requests == ["/v1/models"]


def test_connection_refusal_names_the_launcher_and_its_origin(tmp_path):
    # A bound, non-listening socket reserves a local port that refuses connects.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        path = launcher(tmp_path, origin)
        result = check(path)
    assert result["status"] == "refusal"
    assert result["answered"] is False
    assert result["timed_out"] is False
    assert result["launcher_path"] == str(path)
    assert result["origin"] == origin
    assert str(path) in result["detail"] and origin in result["detail"]


def test_no_endpoints_declaration_does_not_query_or_refuse(tmp_path):
    path = tmp_path / "must-not-execute"
    result = check_serving_origin({}, path, origin_args=["--print-origin"])
    assert result["status"] == "not-applicable"
    assert result["launcher_path"] == str(path)
    assert result["origin"] is None
    assert result["answered"] is None


def test_origin_that_never_answers_honours_timeout(tmp_path):
    with origin_server(silent=True) as (origin, requests):
        path = launcher(tmp_path, origin)
        started = time.monotonic()
        result = check(path, timeout=2.5)
        elapsed = time.monotonic() - started
    assert requests == ["/v1/models"]  # The instrument reached the unresponsive origin.
    assert result["status"] == "refusal"
    assert result["origin"] == origin
    assert result["launcher_path"] == str(path)
    assert result["answered"] is False
    assert result["timed_out"] is True
    assert result["timeout_seconds"] == 2.5
    assert 2.4 <= elapsed < 3.2


def test_discovery_and_http_share_one_deadline(tmp_path):
    with origin_server(silent=True) as (origin, requests):
        path = launcher(tmp_path, origin, delay=1.5)
        started = time.monotonic()
        result = check(path, timeout=4.0)
        elapsed = time.monotonic() - started
    assert requests == ["/v1/models"]
    assert result["timed_out"] is True
    assert result["status"] == "refusal"
    assert 3.9 <= elapsed < 4.8


def test_launcher_discovery_is_also_bounded(tmp_path):
    path = launcher(tmp_path, "http://127.0.0.1:1", delay=5)
    started = time.monotonic()
    result = check(path, timeout=0.1)
    assert time.monotonic() - started < 0.8
    assert result["status"] == "unknown"
    assert result["timed_out"] is True
    assert result["origin"] is None


def test_missing_safe_query_never_executes_launcher(tmp_path):
    result = check_serving_origin(
        {"endpoints_document": "declared.json"}, tmp_path / "must-not-execute"
    )
    assert result["status"] == "unknown"
    assert "no safe origin-only" in result["detail"]


@pytest.mark.parametrize("status", [401, 404, 503])
def test_http_error_is_an_answer_but_not_serving(tmp_path, status):
    with origin_server(status=status) as (origin, requests):
        result = check(launcher(tmp_path, origin))
    assert requests == ["/v1/models"]
    assert result["status"] == "refusal"
    assert result["answered"] is True
    assert result["http_status"] == status


def test_redirect_does_not_substitute_another_origin(tmp_path):
    with (
        origin_server() as (destination, destination_requests),
        origin_server(status=302, redirect=destination) as (origin, requests),
    ):
        result = check(launcher(tmp_path, origin))
    assert requests == ["/v1/models"]
    assert destination_requests == []
    assert result["status"] == "refusal"
    assert result["answered"] is True
    assert result["http_status"] == 302
    assert result["origin"] == origin


@pytest.mark.parametrize(
    "payload",
    [
        "not JSON",
        "[]",
        '{"origin": "file:///tmp/example"}',
        '{"origin": "http://name:secret@127.0.0.1"}',
        '{"origin": "http://127.0.0.1/path"}',
        '{"origin": "http://127.0.0.1:invalid"}',
        '{"origin": "http://127.0.0.1\\n"}',
    ],
)
def test_malformed_query_is_unknown_without_probing(tmp_path, payload):
    result = check(launcher(tmp_path, None, payload=payload))
    assert result["status"] == "unknown"
    assert result["origin"] is None
    assert result["answered"] is None
    assert "secret" not in result["detail"]


def test_failed_query_is_not_serving_even_with_valid_stdout(tmp_path):
    result = check(launcher(tmp_path, "http://127.0.0.1:1", exit_status=2))
    assert result["status"] == "unknown"
    assert "exited 2" in result["detail"]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_refused(tmp_path, timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        check(tmp_path / "unused", timeout=timeout)
