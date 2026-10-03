"""GET /crew reads each run's phase from the crew live view's classification.

A dispatched run exists as a pointer before its worker exists, so the stream
file the route once aged a run against is absent for the first seconds of its
life. The route reads the same classification the crew live view reads for the
same pointer, so such a run reads as launching rather than idle.
"""

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
from reckon.crew.recovery import classify_pointer

PLAN = "visible-work"


def _project(tmp_path: Path, name: str = "sample") -> Path:
    repo = tmp_path / name
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{name}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-sprint" content="current">'
        '<meta name="plan-effort-hours" content="2.0">'
        "<title>Visible work</title>"
        "</head><body></body></html>",
        encoding="utf-8",
    )
    return repo


def _live_dir(config_home: Path) -> Path:
    return config_home / "crew" / "live"


def _write_pointer(config_home: Path, run_id: str, **fields: object) -> dict:
    """Write one live pointer, in the shape a supervised dispatch writes."""
    pointer: dict = {
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
        "node": {
            "plan": PLAN,
            "section": "delivery",
            "role": "implement",
            "done_when": "the route reports the run's phase",
        },
    }
    pointer.update(fields)
    path = _live_dir(config_home) / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")
    return pointer


def _write_stream(config_home: Path, run_id: str, event: dict) -> Path:
    stream = config_home / "crew" / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return stream


def _get(port: int, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture()
def crew_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A server serving one fixture project, over a temporary crew home."""
    config_home = tmp_path / "config"
    state_root = config_home / "state"
    config_home.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    repo = _project(tmp_path)
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({"sample": str(repo / "docs")}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    serve._DISC_CACHE.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"config_home": config_home, "port": server.server_port, "repo": repo}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve._DISC_CACHE.clear()


def _row(payload: dict, run_id: str) -> dict:
    return next(row for row in payload["runs"] if row["run_id"] == run_id)


def test_a_launched_run_reads_the_live_view_phase_not_idle(crew_server) -> None:
    config_home = crew_server["config_home"]
    # A launch writes the pointer and names the stream path before the worker
    # exists, so the file the route once aged a run against is absent.
    stream = config_home / "crew" / "runs" / "run-launched" / "stream.jsonl"
    pointer = _write_pointer(
        config_home,
        "run-launched",
        phase="starting",
        pid=os.getpid(),
        log_path=str(stream),
    )
    assert not stream.exists()

    started = time.perf_counter()
    status, payload = _get(crew_server["port"], "/crew")
    elapsed = time.perf_counter() - started
    print(f"\nGET /crew over the fixture: {elapsed * 1000:.1f} ms", flush=True)

    assert status == 200
    assert serve.crew.crew_home() == (config_home / "crew").resolve()
    expected = str(classify_pointer(pointer).get("phase") or "")
    # The fixture is a genuine launched run: the live view's own classification
    # has a phase for this run and it is not the route's idle word.
    assert expected not in ("", "idle")
    assert _row(payload, "run-launched")["phase"] == expected


def test_a_fresh_stream_reads_working(crew_server) -> None:
    config_home = crew_server["config_home"]
    stream = _write_stream(config_home, "run-fresh", {"type": "thread.started"})
    os.utime(stream, None)
    _write_pointer(config_home, "run-fresh", log_path=str(stream))

    status, payload = _get(crew_server["port"], "/crew/sample")

    assert status == 200
    assert _row(payload, "run-fresh")["phase"] == "working"


def test_a_terminal_stream_reads_done(crew_server) -> None:
    config_home = crew_server["config_home"]
    stream = _write_stream(
        config_home, "run-done", {"type": "turn.completed", "usage": {}}
    )
    _write_pointer(config_home, "run-done", log_path=str(stream))

    status, payload = _get(crew_server["port"], "/crew/sample")

    assert status == 200
    assert _row(payload, "run-done")["phase"] == "done"
