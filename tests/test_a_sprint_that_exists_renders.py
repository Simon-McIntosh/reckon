"""A sprint whose file exists renders on the sprint route.

The sprint view matches the route's sprint id against the payload's sprint
rows, so the route's payload is the thing that decides whether a sprint
renders or the view answers that there is none. A distributed project stores
each sprint as docs/sprints/<id>.html, and the discovery route must carry a
row read from that file, not an empty list.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reckon import serve

PROJECT = "sample"
SPRINT = "current"
THEME = "Probe theme"


def _sprint_document(project: str, sprint: str) -> str:
    state = {
        "type": "sprint",
        "id": sprint,
        "status": "active",
        "theme": THEME,
        "items": [],
        "version": 1,
    }
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="reckon-type" content="sprint">'
        f'<meta name="reckon-id" content="{sprint}">'
        '<meta name="reckon-version" content="1">'
        f"<title>{sprint}</title></head><body>"
        f'<main class="reckon-resource" data-type="sprint" data-id="{sprint}">'
        '<ol data-reckon="sprint-items"></ol>'
        '<script type="application/json" id="reckon-resource-state">'
        f"{json.dumps(state)}</script>"
        "</main></body></html>"
    )


def _project(tmp_path: Path) -> Path:
    """A distributed project whose sprint store is one file on disk.

    The completion marker makes docs/sprints/*.html canonical, and the marker's
    resource list deliberately omits the sprint so the row can only arrive by
    the scan of the sprint directory itself.
    """
    repo = tmp_path / PROJECT
    docs = repo / "docs"
    (docs / ".reckon").mkdir(parents=True)
    (docs / "state" / PROJECT).mkdir(parents=True)
    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps(
            {
                "format": "distributed",
                "status": "complete",
                "project": PROJECT,
                "resources": [],
            }
        ),
        encoding="utf-8",
    )
    (docs / "sprints").mkdir()
    (docs / "sprints" / f"{SPRINT}.html").write_text(
        _sprint_document(PROJECT, SPRINT), encoding="utf-8"
    )
    return repo


@pytest.fixture()
def sprint_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A server serving one fixture project, over a temporary crew home."""
    config_home = tmp_path / "config"
    state_root = config_home / "state"
    config_home.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    repo = _project(tmp_path)
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    serve._DISC_CACHE.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve._DISC_CACHE.clear()


def _discover(port: int, project: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", f"/_discover/{project}")
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_a_sprint_that_exists_renders_on_its_route(sprint_server: int) -> None:
    status, payload = _discover(sprint_server, PROJECT)

    assert status == 200
    rows = payload.get("sprints")
    # An empty list is the no-sprint answer the view renders as such.
    assert isinstance(rows, list) and rows, "the route answered with no sprint"
    # The view matches the route's requested sprint id against these rows.
    row = next((row for row in rows if row.get("id") == SPRINT), None)
    assert row is not None, f"sprint {SPRINT!r} missing from {rows!r}"
    # The row is the stored sprint, not a stub synthesised from a plan's label.
    assert row.get("theme") == THEME
    assert row.get("status") == "active"