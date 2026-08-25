import json
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "docs" / "ui" / "state-loader.js"


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
