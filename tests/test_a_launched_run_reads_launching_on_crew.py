"""The crew route shows a new dispatch promptly while its stream is absent."""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import serve


@pytest.fixture()
def crew_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "config"
    state_root = home / "state"
    plans = tmp_path / "sample" / "docs" / "plans"
    plans.mkdir(parents=True)
    state_root.mkdir(parents=True)
    (plans / "visible-work.html").write_text(
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="visible-work">'
        '<meta name="plan-status" content="active">',
        encoding="utf-8",
    )
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({"sample": str(plans.parent)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"home": home, "port": server.server_port}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _write_pointer(
    home: Path, run_id: str, stream: Path, *, phase: str = "starting"
) -> None:
    pointer = {
        "run_id": run_id,
        "project": "sample",
        "member": "observer",
        "role": "implement",
        "backend": "local",
        "dialect": "codex",
        "argv": ["codex", "exec"],
        "agent": {"model": "frontier", "effort": "high"},
        "created_at": datetime.now(tz=UTC).isoformat(),
        "launcher_host": socket.gethostname(),
        "phase": phase,
        "pid": os.getpid(),
        "log_path": str(stream),
        "node": {"plan": "visible-work", "section": "delivery", "role": "implement"},
    }
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{run_id}.json").write_text(json.dumps(pointer), encoding="utf-8")


def _crew_response(port: int) -> tuple[float, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    started = time.perf_counter()
    try:
        connection.request("GET", "/crew")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
    finally:
        connection.close()
    return time.perf_counter() - started, payload


def test_first_response_shows_a_dispatched_run_without_a_stream(crew_server) -> None:
    home = crew_server["home"]
    stream = home / "crew" / "runs" / "run-new" / "stream.jsonl"
    _write_pointer(home, "run-new", stream)
    assert not stream.exists()

    _, payload = _crew_response(crew_server["port"])

    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["run_id"] == "run-new"
    assert payload["runs"][0]["phase"] in {"launching", "starting", "working"}
    assert not stream.exists()


def test_crew_responds_within_one_second_over_forty_live_pointers(
    crew_server,
) -> None:
    home = crew_server["home"]
    for index in range(40):
        run_id = f"run-{index:02d}"
        stream = home / "crew" / "runs" / run_id / "stream.jsonl"
        _write_pointer(
            home, run_id, stream, phase="starting" if index == 0 else "working"
        )
        assert not stream.exists()

    elapsed, payload = _crew_response(crew_server["port"])
    assert len(payload["runs"]) == 40
    print(f"GET /crew over 40 live pointers: {elapsed:.3f} s", flush=True)
    assert elapsed < 1.0
