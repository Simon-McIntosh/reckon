"""A refused plan page must cost only its own project's row.

The rollup at ``/_projects/index.json`` builds one row per mounted project.
A plan page the parser refuses raises while that project's row is being built;
if the row handler does not isolate the refusal, the exception escapes the
executor and the request thread dies, so one malformed page in one mount
empties every other project's row as well. These cases drive the served
handler over three synthesised mounts — one of them holding a page the parser
refuses — and require the two good rows to survive with the refused project
marked, its error naming the file.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from reckon import crew as crew_module
from reckon import serve
from reckon.project_state import ProjectStateError

_GOOD_BODY = "<h2 id='s1'>Section</h2>"
# A section record carrying an id and no data-id, with no adjacent h2: the
# record contract the parser enforces, in the shape the served rollup met.
_REFUSED_BODY = "<section data-reckon='section' id='s1' data-status='active'></section>"
_REFUSED_FILE = "migration-project-state.html"
_MOUNTS = ("good-a", "bad", "good-b")
_ANSWER_WITHIN_S = 20.0
_POLL_S = 0.05


def _page(project: str, slug: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='docs-project' content='{project}'>"
        "<meta name='reckon-type' content='plan'>"
        f"<meta name='plan-slug' content='{slug}'>"
        "<meta name='plan-title' content='{slug}'>"
        "<meta name='plan-status' content='active'></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(port: int, path: str) -> tuple[int, dict]:
    """Return the status and decoded body for one request, or raise."""

    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return int(response.status), json.loads(response.read().decode())


def _await_bind(port: int) -> None:
    deadline = time.monotonic() + _ANSWER_WITHIN_S
    while time.monotonic() < deadline:
        try:
            _get_json(port, "/_projects/mounts.json")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(_POLL_S)
    raise AssertionError("the served port never answered within the bound")


def _real_config_home_fingerprint() -> list[tuple[str, int]]:
    """Top-level entries of the real config home, minus the harness's own runs.

    The rollup resolves every path through the environment, so nothing here
    should move. The harness churns ``crew/`` for the live run during a test,
    so it is excluded rather than compared.
    """

    home = Path.home() / ".config" / "reckon"
    if not home.exists():
        return []
    return sorted(
        (entry.name, entry.stat().st_mtime_ns)
        for entry in home.iterdir()
        if entry.name != "crew"
    )


@pytest.fixture()
def served_rollup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Serve three synthesised mounts, one holding a refused plan page."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts: dict[str, Path] = {}
    for name in _MOUNTS:
        docs = tmp_path / name / "docs"
        (docs / "plans").mkdir(parents=True)
        (docs / "plans" / "one.html").write_text(_page(name, "one", _GOOD_BODY))
        mounts[name] = docs
    (tmp_path / "bad" / "docs" / "plans" / "migration-project-state.html").write_text(
        _page("bad", "migration-project-state", _REFUSED_BODY)
    )
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(
        json.dumps({name: str(path) for name, path in mounts.items()})
    )

    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(config_home / "state"))
    # main() writes these module globals; restore them on teardown so a served
    # thread's configuration does not leak into sibling tests.
    monkeypatch.setattr(serve, "_MOUNTS_FILE", None)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    monkeypatch.setattr(serve, "_FLEET_WATCH", None)
    monkeypatch.setattr(crew_module, "list_live", lambda **kwargs: [])
    # Keep the case off every real directory and off the tree-walking watch.
    monkeypatch.setattr(serve, "start_fleet_change_watch", lambda *a, **k: None)

    servers: list = []

    class _RecordingServer(serve.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(serve, "ThreadingHTTPServer", _RecordingServer)

    port = _free_port()
    thread = threading.Thread(
        target=serve.main,
        kwargs={"port": port, "host": "127.0.0.1", "mounts_file": mounts_file},
        daemon=True,
        name="served-rollup",
    )
    thread.start()
    _await_bind(port)
    try:
        yield port, mounts
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        thread.join(timeout=5)


def test_refused_page_is_isolated_to_its_project(served_rollup):
    port, _mounts = served_rollup
    before = _real_config_home_fingerprint()

    status, payload = _get_json(port, "/_projects/index.json")

    assert status == 200
    rows = {row["project"]: row for row in payload["projects"]}
    assert set(rows) == set(_MOUNTS)
    for name in ("good-a", "good-b"):
        assert "error" not in rows[name]
        assert rows[name]["data"]["projects"][0]["project"] == name
    assert "error" in rows["bad"]
    assert _REFUSED_FILE in rows["bad"]["error"]
    assert "record must be adjacent to its h2" in rows["bad"]["error"]

    assert _real_config_home_fingerprint() == before


def test_base_except_clause_fails_the_request(
    served_rollup, monkeypatch: pytest.MonkeyPatch
):
    """Negative control: without the isolation the refusal empties the rollup."""

    port, _mounts = served_rollup
    monkeypatch.setattr(
        serve,
        "_PROJECT_ROW_ISOLATED_ERRORS",
        (OSError, ProjectStateError),
    )

    failed = False
    try:
        status, _payload = _get_json(port, "/_projects/index.json")
        failed = status != 200
    except (urllib.error.URLError, OSError):
        failed = True
    assert failed
