from __future__ import annotations

import http.client
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reckon import crew, ledger
import reckon.serve as serve


def _add_project(root: Path, name: str, mounts: dict[str, str]) -> Path:
    repo = root / name
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "visible-work.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{name}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="visible-work">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-sprint" content="current">'
        "<title>Visible work</title>"
        '</head><body><main class="plan-doc"></main></body></html>',
        encoding="utf-8",
    )
    ledger.register_member(
        name,
        "observer",
        harness="codex",
        role="review",
        root=repo,
    )
    mounts[name] = str(repo / "docs")
    return repo


@pytest.fixture()
def crew_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    mounts_file = config_home / "mounts.json"
    state_root = config_home / "state"
    config_home.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    mounts: dict[str, str] = {}
    repos = {name: _add_project(tmp_path, name, mounts) for name in ("reckon", "other")}
    mounts_file.write_text(json.dumps(mounts), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    serve._DISC_CACHE.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "config_home": config_home,
            "mounts": mounts,
            "mounts_file": mounts_file,
            "port": server.server_port,
            "repos": repos,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve._DISC_CACHE.clear()


def _write_pointer(
    config_home: Path,
    run_id: str,
    *,
    project: str = "reckon",
    event: dict | None = None,
) -> Path:
    log_path = config_home / "crew" / "runs" / run_id / "stream.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(json.dumps(event or {"type": "thread.started"}) + "\n")
    pointer = {
        "run_id": run_id,
        "project": project,
        "member": "observer",
        "role": "implement",
        "backend": "local",
        "dialect": "codex",
        "argv": ["codex", "exec"],
        "agent": {"model": "frontier", "effort": "high"},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "log_path": str(log_path),
        "node": {
            "plan": "visible-work",
            "section": "delivery",
            "role": "implement",
            "done_when": "the route returns the live run",
        },
    }
    live_path = config_home / "crew" / "live" / f"{run_id}.json"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(json.dumps(pointer), encoding="utf-8")
    return log_path


def _get(port: int, path: str) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body.decode()
        return response.status, payload
    finally:
        connection.close()


def test_all_projects_route_joins_roster_and_navigation(crew_server) -> None:
    _write_pointer(crew_server["config_home"], "run-all")

    status, payload = _get(crew_server["port"], "/crew")

    assert status == 200
    assert payload["project"] is None
    assert len(payload["runs"]) == 1
    row = payload["runs"][0]
    assert {
        "run_id": row["run_id"],
        "project": row["project"],
        "member": row["member"],
        "role": row["role"],
        "plan": row["plan"],
        "section": row["section"],
        "phase": row["phase"],
        "plan_href": row["plan_href"],
        "sprint_href": row["sprint_href"],
    } == {
        "run_id": "run-all",
        "project": "reckon",
        "member": "observer",
        "role": "review",
        "plan": "visible-work",
        "section": "delivery",
        "phase": "working",
        "plan_href": "/reckon/#plan/visible-work",
        "sprint_href": "/reckon/#sprint/current",
    }
    assert row["last_activity"].endswith("Z")
    assert row["model"] == "frontier"
    assert row["effort"] == "high"
    assert row["elapsed_seconds"] >= 0
    assert row["gate"] == "the route returns the live run"


def test_project_route_filters_other_mounted_runs(crew_server) -> None:
    _write_pointer(crew_server["config_home"], "run-reckon")
    _write_pointer(
        crew_server["config_home"],
        "run-other",
        project="other",
    )

    all_status, all_payload = _get(crew_server["port"], "/crew")
    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert all_status == status == 200
    assert {row["project"] for row in all_payload["runs"]} == {"reckon", "other"}
    assert payload["project"] == "reckon"
    assert [row["run_id"] for row in payload["runs"]] == ["run-reckon"]


def test_terminal_event_reports_done(crew_server) -> None:
    _write_pointer(
        crew_server["config_home"],
        "run-done",
        event={"type": "turn.completed", "usage": {}},
    )

    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert status == 200
    assert payload["runs"][0]["phase"] == "done"


def test_recent_nonterminal_stream_reports_working(crew_server) -> None:
    log_path = _write_pointer(crew_server["config_home"], "run-recent")
    os.utime(log_path, None)

    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert status == 200
    assert payload["runs"][0]["phase"] == "working"


def test_stale_nonterminal_stream_reports_idle(crew_server) -> None:
    log_path = _write_pointer(crew_server["config_home"], "run-stale")
    stale = time.time() - crew.LOG_STALE_AFTER_SECONDS - 5
    os.utime(log_path, (stale, stale))

    status, payload = _get(crew_server["port"], "/crew/reckon")

    assert status == 200
    assert payload["runs"][0]["phase"] == "idle"


def test_log_tail_read_is_bounded_by_byte_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = tmp_path / "stream.jsonl"
    line = json.dumps({"type": "thread.started", "padding": "x" * 256}) + "\n"
    stream.write_text(line * (serve.CREW_LOG_TAIL_BYTES // len(line) + 10))
    actual_open = Path.open
    read_sizes: list[int] = []

    class TrackedReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def seek(self, offset: int):
            return self.handle.seek(offset)

        def read(self, size: int = -1):
            read_sizes.append(size)
            return self.handle.read(size)

    def tracked_open(path: Path, *args, **kwargs):
        handle = actual_open(path, *args, **kwargs)
        return TrackedReader(handle) if path == stream else handle

    monkeypatch.setattr(Path, "open", tracked_open)

    lines = serve._read_log_tail(stream)

    assert stream.stat().st_size > serve.CREW_LOG_TAIL_BYTES
    assert read_sizes == [serve.CREW_LOG_TAIL_BYTES]
    assert lines


def test_unknown_project_route_is_not_found(crew_server) -> None:
    status, payload = _get(crew_server["port"], "/crew/missing")

    assert status == 404
    assert payload == "project not found"
