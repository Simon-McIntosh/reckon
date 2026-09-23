"""The served loader assembles window.STATE from discovery alone.

The served path has a live ``/_discover/<project>`` endpoint that already
carries the inventory, sprints, milestones, blockers, timeline, active sprint,
north stars, source format and resource versions. The aggregate file the fleet
migration superseded is not read for such a page: transferring it beside the
discovery payload moved the inventory twice and put a stale ``projects[]``
block into ``window.STATE``. ``state/<project>/projection.json`` stands in only
for the static build, and ``state/<project>/index.json`` only when projection is
also unavailable.

Each case runs the real loader under ``node`` with a recording ``fetch``, so
the assertions are about the requests the loader actually made and the state
it actually assembled, not about the source text.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "docs" / "ui" / "state-loader.js"

PROJECT = "sample"
DISCOVERY_ENDPOINT = f"/_discover/{PROJECT}"

# Fields the persisted block carries and the synthesised row never does, so a
# leaked projects[] entry is visible rather than blended into the live counts.
PERSISTED_MARKER = "persisted_projects_count"

DISCOVERY = {
    "source_format": "distributed",
    "active_sprint_id": "focus",
    "sprints": [{"id": "focus", "status": "active", "items": []}],
    "milestones": [{"id": "m-discovery", "title": "discovery milestone"}],
    "timeline": [{"id": "t-discovery"}],
    "blockers": [{"slug": "held"}],
    "north_stars": [{"id": "north"}],
    "resource_versions": {"project:project": 3},
    "inventory": [{"slug": "work", "type": "plan", "status": "active"}],
}

PERSISTED_INDEX = {
    "data": {
        "active_sprint_id": "from-index",
        "sprints": [{"id": "from-index", "status": "active", "items": []}],
        "milestones": [{"id": "m-index"}],
        "timeline": [{"id": "t-index"}],
        "inventory": [{"slug": "index-only", "type": "plan", "status": "active"}],
        "projects": [
            {
                "project": PROJECT,
                "published": "persisted-only",
                PERSISTED_MARKER: 7,
            }
        ],
    }
}


def _run(url_responses: dict) -> dict:
    """Run the loader under node with a recording fetch.

    ``url_responses`` maps a URL to a JavaScript expression evaluating to the
    response object, or ``None`` to model a refusal at the network level (the
    fetch promise rejects).
    """
    branches = []
    for url, expression in url_responses.items():
        if expression is None:
            body = f'    if (url === {json.dumps(url)}) throw new Error("refused");'
        else:
            body = (
                f"    if (url === {json.dumps(url)}) {{\n"
                f"      requested.push(url);\n"
                f"      return {expression};\n"
                f"    }}"
            )
        branches.append(body)
    script = f"""
const fs = require("fs");
global.window = {{ location: {{ pathname: "/{PROJECT}/" }} }};
global.document = {{ querySelector: () => ({{ content: {json.dumps(PROJECT)} }}) }};
const requested = [];
const ok = (payload) => ({{ ok: true, status: 200, json: async () => payload }});
const missing = () => ({{ ok: false, status: 404, json: async () => ({{}}) }});
global.fetch = async (url) => {{
{chr(10).join(branches)}
  throw new Error("unexpected fetch " + url);
}};
eval(fs.readFileSync({json.dumps(str(LOADER))}, "utf8"));
window.STATE_READY.then(
  state => console.log(JSON.stringify({{
    resolved: true,
    requested,
    sprints: state.sprints.map(sprint => sprint.id),
    active_sprint_id: state.active_sprint_id,
    timeline: state.timeline.map(entry => entry.id),
    inventory: state.inventory.map(entry => entry.slug),
    projects_zero: state.projects[0],
  }})),
  error => console.log(JSON.stringify({{
    resolved: false,
    requested,
    message: error.message,
  }}))
);
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _aggregate_requests(requested: list[str]) -> list[str]:
    return [url for url in requested if url.endswith(("index.json", "projection.json"))]


def test_discovery_answers_so_no_aggregate_is_read() -> None:
    observed = _run(
        {
            f"state/{PROJECT}/projection.json": "missing()",
            f"state/{PROJECT}/index.json": f"ok({json.dumps(PERSISTED_INDEX)})",
            DISCOVERY_ENDPOINT: f"ok({json.dumps(DISCOVERY)})",
        }
    )

    assert observed["resolved"] is True, observed
    assert _aggregate_requests(observed["requested"]) == [], (
        "the served loader requested an aggregate file: "
        f"{observed['requested']} — index.json is requested only when discovery "
        "is unreachable"
    )

    assert observed["sprints"] == ["focus"]
    assert observed["active_sprint_id"] == "focus"
    assert observed["timeline"] == ["t-discovery"]
    assert observed["inventory"] == ["work"]

    row = observed["projects_zero"]
    assert row["project"] == PROJECT
    assert PERSISTED_MARKER not in row, (
        "projects[0] took a field from the persisted projects[] block while "
        "discovery answered"
    )
    assert row["published"] == ""


def test_discovery_refused_falls_back_to_projection() -> None:
    observed = _run(
        {
            DISCOVERY_ENDPOINT: None,
            f"state/{PROJECT}/projection.json": (
                f"ok({json.dumps({'data': {'sprints': [{'id': 'from-projection'}]}})})"
            ),
            f"state/{PROJECT}/index.json": f"ok({json.dumps(PERSISTED_INDEX)})",
        }
    )

    assert observed["resolved"] is True, observed
    assert f"state/{PROJECT}/projection.json" in observed["requested"]
    assert f"state/{PROJECT}/index.json" not in observed["requested"]
    assert observed["sprints"] == ["from-projection"]


def test_discovery_refused_and_no_projection_falls_back_to_index() -> None:
    observed = _run(
        {
            DISCOVERY_ENDPOINT: None,
            f"state/{PROJECT}/projection.json": "missing()",
            f"state/{PROJECT}/index.json": (
                f"ok({json.dumps({'data': {'sprints': [{'id': 'from-index'}]}})})"
            ),
        }
    )

    assert observed["resolved"] is True, observed
    assert f"state/{PROJECT}/index.json" in observed["requested"]
    assert observed["sprints"] == ["from-index"]


def test_a_discovery_server_failure_is_not_hidden_by_a_projection() -> None:
    observed = _run(
        {
            DISCOVERY_ENDPOINT: "({ ok: false, status: 500, json: async () => ({}) })",
            f"state/{PROJECT}/projection.json": (
                f"ok({json.dumps({'data': {'sprints': [{'id': 'from-projection'}]}})})"
            ),
        }
    )

    assert observed["resolved"] is False
    assert observed["message"] == f"{DISCOVERY_ENDPOINT} returned HTTP 500"
