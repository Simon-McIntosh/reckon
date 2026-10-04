"""A served project whose sprint file exists renders that sprint, not "No sprint.".

The sprint view decides by matching the route's sprint id against the rows
of the served discovery payload; an empty match renders the view's
"No sprint." placeholder. The gate drives that whole chain for one fixture
project: the server discovers the sprint from its file on disk, and the
authored sprint view, executed as shipped, returns the sprint surface
instead of the placeholder.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reckon import serve
from tests import spa_module_eval

ROOT = Path(__file__).resolve().parents[1]
SPRINT_VIEW = ROOT / "docs" / "ui" / "sprint.jsx"

PROJECT = "sample"
SPRINT = "delivery"
THEME = "Probe theme"

# Reads the element tree the view returns; the placeholder is the only
# branch that produces the "No sprint." element, so its absence is the
# render having happened.
_RENDER_EXPRESSION = """
(() => {
  const root = window.Sprint({ sprintId: %(sprint)s, onNav: () => {} });
  const text = [];
  const walk = node => {
    if (node === null || node === undefined || typeof node === "boolean") return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (typeof node === "object" && node.__element) { walk(node.children); return; }
    text.push(String(node));
  };
  walk(root);
  const classes = String((root && root.props && root.props.className) || "");
  return {
    rootClass: classes,
    sprintSurface: classes.includes("r-sprint-surface"),
    placeholder: classes === "r-page" && text.join(" ").includes("No sprint."),
    text: text.join(" "),
  };
})()
""" % {"sprint": json.dumps(SPRINT)}


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

    The completion marker makes docs/sprints/*.html canonical and its
    resource list omits the sprint, so the row can only arrive by the scan
    of the sprint directory itself.
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
    mounts_file.write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
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


def _get(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _render_sprint_route(tmp_path: Path, payload: dict) -> dict:
    """Run the authored sprint view against the payload its route serves."""
    harness = tmp_path / "sprint_route_probe.jsx"
    harness.write_text(
        "const React = {\n"
        "  createElement: (type, props, ...children) => "
        "({ __element: true, type, props: props || {}, children }),\n"
        "  useMemo: factory => factory(),\n"
        "  useState: initial => [initial, () => {}],\n"
        "  useEffect: () => {},\n"
        "};\n"
        "const document = { querySelector: () => null };\n"
        f"window.STATE = {json.dumps(payload)};\n"
        f"{SPRINT_VIEW.read_text(encoding='utf-8')}\n",
        encoding="utf-8",
    )
    return spa_module_eval.evaluate_jsx_module(harness, _RENDER_EXPRESSION)


def test_a_sprint_file_renders_on_its_sprint_route(
    sprint_server: int, tmp_path: Path
) -> None:
    # The route's own page: the server answers the fixture project at it.
    page_status, page = _get(sprint_server, f"/{PROJECT}/")
    assert page_status == 200
    assert f'content="{PROJECT}"' in page.decode("utf-8")

    # The data request the view on that route performs. The sprint id
    # travels in the URL fragment, which a client never sends.
    status, body = _get(sprint_server, f"/_discover/{PROJECT}")
    assert status == 200
    payload = json.loads(body)
    assert not payload.get("error"), payload

    rendered = _render_sprint_route(tmp_path, payload)

    assert rendered["sprintSurface"], rendered
    assert not rendered["placeholder"], rendered
    # The surface is the fixture's sprint, not another one.
    assert SPRINT in rendered["text"], rendered
    assert THEME in rendered["text"], rendered