from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reckon import ledger
import reckon.serve as serve


@pytest.fixture()
def finished_run_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    mounts_file = config_home / "mounts.json"
    state_root = config_home / "state"
    repository = tmp_path / "repository"
    docs = repository / "docs"
    docs.mkdir(parents=True)
    config_home.mkdir()
    state_root.mkdir()
    mounts_file.write_text(json.dumps({"reckon": str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": server.server_port, "repository": repository}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(port: int, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_plan_finished_runs_are_newest_first_and_empty_when_absent(
    finished_run_server,
) -> None:
    repository = finished_run_server["repository"]
    older = ledger.build_record(
        run_id="run-older",
        plan="visible-work",
        section="delivery",
        node="older-node",
        gate="passed",
        dispatched_at="2026-08-25T08:00:00Z",
        completed_at="2026-08-25T08:10:00Z",
        commits=["1111111"],
    )
    newer = ledger.build_record(
        run_id="run-newer",
        plan="visible-work",
        section="verification",
        node="newer-node",
        gate="failed",
        dispatched_at="2026-08-25T09:00:00Z",
        completed_at="2026-08-25T09:20:00Z",
        commits=["2222222"],
    )
    unrelated = ledger.build_record(
        run_id="run-unrelated",
        plan="other-work",
        section="delivery",
        node="other-node",
        gate="passed",
        dispatched_at="2026-08-25T10:00:00Z",
        completed_at="2026-08-25T10:05:00Z",
        commits=["3333333"],
    )
    for record in (newer, older, unrelated):
        ledger.append_run("reckon", record, root=repository)

    status, payload = _get(
        finished_run_server["port"], "/crew/reckon/finished/visible-work"
    )

    assert status == 200
    assert payload["project"] == "reckon"
    assert payload["plan"] == "visible-work"
    assert [run["run_id"] for run in payload["runs"]] == [
        "run-newer",
        "run-older",
    ]
    assert [
        {
            "dispatched_at": run["dispatched_at"],
            "completed_at": run["completed_at"],
            "node": run["node"],
            "section": run["section"],
            "gate": run["gate"],
            "commits": run["commits"],
        }
        for run in payload["runs"]
    ] == [
        {
            "dispatched_at": "2026-08-25T09:00:00Z",
            "completed_at": "2026-08-25T09:20:00Z",
            "node": "newer-node",
            "section": "verification",
            "gate": "failed",
            "commits": ["2222222"],
        },
        {
            "dispatched_at": "2026-08-25T08:00:00Z",
            "completed_at": "2026-08-25T08:10:00Z",
            "node": "older-node",
            "section": "delivery",
            "gate": "passed",
            "commits": ["1111111"],
        },
    ]

    empty_status, empty_payload = _get(
        finished_run_server["port"], "/crew/reckon/finished/no-finished-work"
    )
    assert empty_status == 200
    assert empty_payload == {
        "project": "reckon",
        "plan": "no-finished-work",
        "runs": [],
    }
