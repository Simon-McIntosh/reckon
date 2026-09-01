import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from reckon import serve
from tests.spa_browser_harness import authored_shell_source

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "docs" / "ui" / "state-loader.js"
SHELL = authored_shell_source(ROOT)
CREW = ROOT / "docs" / "ui" / "crew.jsx"


def _plan_html(slug: str) -> str:
    return f"""<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="active">
</head><body><main><h2>Work</h2></main></body></html>
"""


def _load_discovery_state(payload: dict) -> dict:
    script = f"""
const fs = require("fs");
global.window = {{location: {{pathname: "/sample/"}}}};
global.document = {{querySelector: () => ({{content: "sample"}})}};
const discovery = {json.dumps(payload)};
global.fetch = async (url) => {{
  if (url === "state/sample/projection.json") {{
    return {{ok: false, status: 404, json: async () => ({{}})}};
  }}
  if (url === "state/sample/index.json") {{
    return {{
      ok: true,
      status: 200,
      json: async () => ({{data: {{active_sprint_id: "focus"}}}}),
    }};
  }}
  if (url === "/_discover/sample") {{
    return {{ok: true, status: 200, json: async () => discovery}};
  }}
  throw new Error("unexpected fetch " + url);
}};
eval(fs.readFileSync({json.dumps(str(LOADER))}, "utf8"));
window.STATE_READY.then(() => console.log(JSON.stringify(window.STATE)));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _load_central_fallback_state(payload: dict) -> dict:
    script = f"""
const fs = require("fs");
global.window = {{location: {{pathname: "/sample/"}}}};
global.document = {{querySelector: () => ({{content: "sample"}})}};
const projection = {json.dumps(payload)};
global.fetch = async (url) => {{
  if (url === "state/sample/projection.json") {{
    return {{ok: true, status: 200, json: async () => projection}};
  }}
  if (url === "/_discover/sample") {{
    return {{ok: false, status: 404, json: async () => ({{}})}};
  }}
  throw new Error("unexpected fetch " + url);
}};
eval(fs.readFileSync({json.dumps(str(LOADER))}, "utf8"));
window.STATE_READY.then(() => console.log(JSON.stringify(window.STATE)));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_project_state_revalidation_reassembles_discovery_inventory() -> None:
    discoveries = [
        {"inventory": [{"slug": "present", "type": "plan", "status": "active"}]},
        {
            "inventory": [
                {"slug": "present", "type": "plan", "status": "active"},
                {"slug": "created-later", "type": "plan", "status": "pending"},
            ]
        },
    ]
    script = f"""
const fs = require("fs");
global.window = {{location: {{pathname: "/sample/"}}}};
global.document = {{querySelector: () => ({{content: "sample"}})}};
const discoveries = {json.dumps(discoveries)};
let discoveryRequests = 0;
global.fetch = async (url) => {{
  if (url === "state/sample/projection.json") {{
    return {{ok: false, status: 404, json: async () => ({{}})}};
  }}
  if (url === "state/sample/index.json") {{
    return {{ok: true, status: 200, json: async () => ({{data: {{}}}})}};
  }}
  if (url === "/_discover/sample") {{
    const payload = discoveries[Math.min(discoveryRequests, discoveries.length - 1)];
    discoveryRequests += 1;
    return {{ok: true, status: 200, json: async () => payload}};
  }}
  throw new Error("unexpected fetch " + url);
}};
eval(fs.readFileSync({json.dumps(str(LOADER))}, "utf8"));
window.STATE_READY.then(async firstState => {{
  const secondState = await window.revalidateProjectState();
  console.log(JSON.stringify({{
    callable: typeof window.revalidateProjectState === "function",
    discoveryRequests,
    firstInventory: firstState.inventory.map(item => item.slug),
    secondInventory: secondState.inventory.map(item => item.slug),
    publishedCurrentState: window.STATE === secondState,
  }}));
}});
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    observation = json.loads(result.stdout)

    assert observation == {
        "callable": True,
        "discoveryRequests": 2,
        "firstInventory": ["present"],
        "secondInventory": ["present", "created-later"],
        "publishedCurrentState": True,
    }


def test_open_page_receives_new_plan_without_navigation_or_polling(
    tmp_path: Path, monkeypatch
) -> None:
    docs = tmp_path / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    (plans / "present.html").write_text(_plan_html("present"), encoding="utf-8")

    monkeypatch.setattr(serve, "load_mounts", lambda: {"sample": docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", docs / "state")
    serve._DISC_CACHE.clear()
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"

    script = f"""
const fs = require("fs");
const http = require("http");
const origin = {json.dumps(origin)};
let documentNavigations = 0;
global.window = {{
  location: {{pathname: "/sample/", reload: () => {{ documentNavigations += 1; }}}},
}};
global.document = {{querySelector: () => ({{content: "sample"}})}};
const nativeFetch = global.fetch;
global.fetch = (url, options) => nativeFetch(new URL(url, origin + "/sample/"), options);

class TestEventSource {{
  constructor(path) {{
    this.listeners = {{}};
    this.request = http.get(origin + path, response => {{
      response.setEncoding("utf8");
      let buffer = "";
      response.on("data", chunk => {{
        buffer += chunk;
        let boundary;
        while ((boundary = buffer.indexOf("\\n\\n")) >= 0) {{
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = frame.split("\\n").find(line => line.startsWith("event: "))?.slice(7);
          for (const listener of this.listeners[event] || []) listener();
        }}
      }});
    }});
  }}
  addEventListener(event, listener) {{
    (this.listeners[event] ||= []).push(listener);
  }}
  close() {{ this.request.destroy(); }}
}}
global.EventSource = TestEventSource;

eval(fs.readFileSync({json.dumps(str(LOADER))}, "utf8"));
window.STATE_READY.then(() => {{
  let invalidationRevision = 0;
  const changes = window.watchProjectStateChanges(async () => {{
    await window.revalidateProjectState();
    invalidationRevision += 1;
    if (window.STATE.inventory.some(item => item.slug === "created-later")) {{
      console.log(JSON.stringify({{
        inventory: window.STATE.inventory.map(item => item.slug),
        invalidationRevision,
        documentNavigations,
      }}));
      changes.close();
    }}
  }});
  changes.addEventListener("ready", () => console.log("READY"));
}});
"""
    process = subprocess.Popen(
        ["node", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        (plans / "created-later.html").write_text(
            _plan_html("created-later"), encoding="utf-8"
        )
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert process.returncode == 0, stderr
    assert json.loads(stdout) == {
        "inventory": ["created-later", "present"],
        "invalidationRevision": 1,
        "documentNavigations": 0,
    }

    loader_source = LOADER.read_text(encoding="utf-8")
    shell_source = SHELL.read_text(encoding="utf-8")
    crew_source = CREW.read_text(encoding="utf-8")
    assert "setInterval(" not in loader_source
    assert "watchProjectStateChanges?.(refreshProjectState)" in shell_source
    assert "const [invRev, setInvRev] = useState(0)" in shell_source
    assert "const CREW_POLL_INTERVAL_MS = 3000" in crew_source
    assert "window.setInterval(poll, CREW_POLL_INTERVAL_MS)" in crew_source


def test_plan_effort_hours_survives_discovery_and_central_fallback() -> None:
    discovery_state = _load_discovery_state(
        {
            "inventory": [
                {
                    "slug": "work",
                    "type": "plan",
                    "status": "active",
                    "effort_hours": 3.25,
                }
            ]
        }
    )
    fallback_state = _load_central_fallback_state(
        {
            "data": {
                "plans": [
                    {
                        "path": "plans/work.html",
                        "type": "plan",
                        "status": "active",
                        "effort_hours": 3.25,
                    }
                ]
            }
        }
    )

    assert discovery_state["plans"]["work"]["effort_hours"] == 3.25
    assert fallback_state["plans"]["work"]["effort_hours"] == 3.25


def test_discovery_load_preserves_receipt_sprints_status_and_attachments() -> None:
    state = _load_discovery_state(
        {
            "source_format": "distributed",
            "resource_versions": {
                "project:project": 3,
                "sprint:focus": 5,
                "sprint:also-active": 2,
            },
            "active_sprint_id": "focus",
            "sprints": [
                {"id": "focus", "status": "active", "items": []},
                {"id": "also-active", "status": "active", "items": []},
                {"id": "later", "status": "planned", "items": []},
            ],
            "milestones": [],
            "inventory": [
                {
                    "slug": "work",
                    "type": "plan",
                    "status": "active",
                    "effective_status": "blocked",
                    "depends_on": ["foundation"],
                },
                {
                    "slug": "background",
                    "type": "research",
                    "status": "done",
                    "informs": ["work"],
                },
                {
                    "slug": "outcome",
                    "type": "evidence",
                    "status": "done",
                    "evidence_for": ["work"],
                    "verifies": ["work#checks"],
                },
            ],
        }
    )

    assert state["source_format"] == "distributed"
    assert state["resource_versions"] == {
        "project:project": 3,
        "sprint:focus": 5,
        "sprint:also-active": 2,
    }
    assert datetime.fromisoformat(state["loaded_at"])

    assert state["active_sprint_id"] == "focus"
    assert [sprint["id"] for sprint in state["active_sprints"]] == [
        "focus",
        "also-active",
    ]
    assert state["active_sprint_conflict"] is True

    work = state["plans"]["work"]
    assert work["status"] == "active"
    assert work["workflow_status"] == "active"
    assert work["effective_status"] == "blocked"
    assert work["depends_on"] == ["foundation"]

    assert state["attachment_relations"] == [
        {
            "relation": "informs",
            "source": "research:background",
            "target": "work",
        },
        {
            "relation": "evidence_for",
            "source": "evidence:outcome",
            "target": "work",
        },
        {
            "relation": "verifies",
            "source": "evidence:outcome",
            "target": "work#checks",
        },
    ]
    assert all(
        relation["relation"] not in {"depends_on", "blocks"}
        for relation in state["attachment_relations"]
    )
