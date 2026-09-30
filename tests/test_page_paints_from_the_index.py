"""The page paints from the index, and derived state arrives afterwards.

The served loader asks for the index first — one row per document and figure
and nothing derived — and resolves the first paint on it. The discovery
payload, which carries the derived fields and the ready set, is fetched
afterwards in the background and merged into the rows already on screen,
without re-sorting them. An index that is unavailable must not cost the page
its rows: the loader then takes the discovery path it used before.

Each case runs the real ``docs/ui/state-loader.js`` under ``node`` with a
recording ``fetch``, so the assertions are about the requests the loader
actually made and the state it actually assembled. A case whose readiness
never comes fails on its timeout rather than hanging the suite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "docs" / "ui" / "state-loader.js"

PROJECT = "sample"
INDEX_ENDPOINT = f"/_index/{PROJECT}"
DISCOVERY_ENDPOINT = f"/_discover/{PROJECT}"

#: Long enough for three HTTP round trips, short enough that the negative
#: control — readiness gated on a discovery response that never comes — fails
#: the case rather than stalling the suite.
READY_TIMEOUT_MS = 2000

# One plan, a second plan that the derived payload marks ready, an evidence
# document, and a figure. The figure has no counterpart in the discovery
# payload, so a merge that rebuilt the list from discovery would drop it.
INDEX_ROWS = [
    {
        "slug": "alpha",
        "href": "plans/alpha",
        "type": "plan",
        "title": "Alpha",
        "status": "active",
        "sprint": "focus",
        "archived": "",
        "created": 100,
        "edited": "2026-01-01T00:00:00",
        "width": None,
        "height": None,
    },
    {
        "slug": "beta",
        "href": "plans/beta",
        "type": "plan",
        "title": "Beta",
        "status": "active",
        "sprint": "focus",
        "archived": "",
        "created": 200,
        "edited": "2026-01-02T00:00:00",
        "width": None,
        "height": None,
    },
    {
        "slug": "gate",
        "href": "evidence/gate",
        "type": "evidence",
        "title": "Gate",
        "status": "",
        "sprint": None,
        "archived": "",
        "created": 50,
        "edited": "2026-01-03T00:00:00",
        "width": None,
        "height": None,
    },
    {
        "slug": "plot.png",
        "href": "figures/plot.png",
        "type": "figure",
        "title": "Plot",
        "status": "",
        "sprint": None,
        "archived": "",
        "created": 60,
        "edited": "2026-01-04T00:00:00",
        "width": 640,
        "height": 480,
    },
]

# The same rows plus what the index deliberately does not carry: the effective
# status each row derives once blockers are known, and the ready set.
DISCOVERY = {
    "source_format": "distributed",
    "active_sprint_id": "focus",
    "sprints": [{"id": "focus", "status": "active", "items": []}],
    "milestones": [{"id": "m1", "title": "M1"}],
    "north_stars": [{"id": "north"}],
    "timeline": [{"id": "t1"}],
    "blockers": [{"slug": "alpha"}],
    "ready_set": {"ready": ["beta"], "total": 1},
    "resource_versions": {"project:project": 3},
    "inventory": [
        {
            "slug": "alpha",
            "type": "plan",
            "title": "Alpha",
            "status": "active",
            "workflow_status": "active",
            "effective_status": "blocked",
            "blockers": 1,
            "impl": 0.4,
            "created": 100,
            "edited": "2026-01-01T00:00:00",
        },
        {
            "slug": "beta",
            "type": "plan",
            "title": "Beta",
            "status": "active",
            "workflow_status": "active",
            "effective_status": "ready",
            "blockers": 0,
            "impl": 0.9,
            "created": 200,
            "edited": "2026-01-02T00:00:00",
        },
        {
            "slug": "gate",
            "type": "evidence",
            "title": "Gate",
            "status": "recorded",
            "effective_status": "recorded",
            "created": 50,
            "edited": "2026-01-03T00:00:00",
        },
    ],
}

#: The order the index listed the rows in, which the merge must not change.
INDEX_ORDER = ["alpha", "beta", "evidence:gate", "figure:plot.png"]

_HARNESS = """
const fs = require("fs");
const PROJECT = __PROJECT__;
const LOADER_PATH = __LOADER__;
const READY_TIMEOUT_MS = __TIMEOUT__;
const DATA = __DATA__;
global.window = { location: { pathname: "/" + PROJECT + "/" } };
global.document = { querySelector: () => ({ content: PROJECT }) };
const requested = [];
const ok = (payload) => ({ ok: true, status: 200, json: async () => payload });
const missing = () => ({ ok: false, status: 404, json: async () => ({}) });
const never = () => new Promise(() => {});
global.fetch = async (url) => {
__BEHAVIOUR__
  throw new Error("unexpected fetch " + url);
};
eval(fs.readFileSync(LOADER_PATH, "utf8"));
const watchdog = setTimeout(() => {
  console.log(JSON.stringify({
    resolved: false,
    timeout: true,
    requested: requested.slice(),
  }));
  process.exit(0);
}, READY_TIMEOUT_MS);
__BODY__
"""

_INDEX_ANSWERS = """
  if (url === "/_index/sample") {
    requested.push(url);
    return ok(DATA.index);
  }
"""

_INDEX_IS_ABSENT = """
  if (url === "/_index/sample") {
    requested.push(url);
    return missing();
  }
"""

_INDEX_REFUSED = """
  if (url === "/_index/sample") {
    requested.push(url);
    throw new Error("refused");
  }
"""

_DISCOVERY_NEVER = """
  if (url === "/_discover/sample") {
    requested.push(url);
    return never();
  }
"""

_DISCOVERY_ANSWERS = """
  if (url === "/_discover/sample") {
    requested.push(url);
    return ok(DATA.discovery);
  }
"""

# The first paint's own summary, shared by the cases that read it.
_SUMMARIZE = """
const summary = state => ({
  keys: state.inventory.map(row => row.nav_key),
  types: state.inventory.map(row => row.type),
  titles: state.inventory.map(row => row.title),
  effective_status: state.inventory.map(row => row.effective_status),
  impl: state.inventory.map(row => row.impl),
  ready_set: state.ready_set,
});
"""


def _run_node(behaviour: str, body: str) -> dict:
    """Run the real loader under node against a recording fetch."""

    script = (
        _HARNESS.replace("__PROJECT__", json.dumps(PROJECT))
        .replace("__LOADER__", json.dumps(str(LOADER)))
        .replace("__TIMEOUT__", str(READY_TIMEOUT_MS))
        .replace(
            "__DATA__",
            json.dumps({"index": INDEX_ROWS, "discovery": DISCOVERY}),
        )
        .replace("__BEHAVIOUR__", behaviour)
        .replace("__BODY__", body)
    )
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def test_readiness_paints_from_the_index_while_discovery_is_outstanding() -> None:
    """The first paint needs /_index alone; /_discover never answers here."""

    body = f"""
{_SUMMARIZE}
window.STATE_READY.then(async state => {{
  clearTimeout(watchdog);
  const atReadiness = requested.slice();
  // Give the deferred derived fetch its turn so the snapshot below shows the
  // request the loader went on to make, not merely one the process outran.
  await new Promise(resolve => setTimeout(resolve, 100));
  console.log(JSON.stringify({{
    resolved: true,
    timeout: false,
    requested_at_readiness: atReadiness,
    requested_after: requested.slice(),
    paint: summary(state),
    inventory_size: state.inventory.length,
  }}));
}});
"""
    observed = _run_node(_INDEX_ANSWERS + _DISCOVERY_NEVER, body)

    assert observed["resolved"] is True, (
        "readiness did not arrive on the index alone: "
        f"{observed} — the page waited on the discovery response"
    )
    assert observed["requested_at_readiness"] == [INDEX_ENDPOINT], (
        "the first paint asked for more than the index: "
        f"{observed['requested_at_readiness']}"
    )
    # The background fetch is deferred past readiness, not skipped: it is the
    # positive control for the assertion above.
    assert observed["requested_after"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT]

    paint = observed["paint"]
    assert paint["keys"] == INDEX_ORDER
    assert observed["inventory_size"] == len(INDEX_ROWS)
    assert paint["titles"] == ["Alpha", "Beta", "Gate", "Plot"]
    assert paint["types"] == ["plan", "plan", "evidence", "figure"]
    # Nothing derived has arrived yet, so the ready set is still empty and the
    # effective status is only the row's own workflow status.
    assert paint["ready_set"] == {}
    assert paint["effective_status"][:2] == ["active", "active"]


def test_derived_fields_merge_into_the_rows_already_on_screen() -> None:
    """The discovery payload lands on the painted rows, in place and in order."""

    body = f"""
{_SUMMARIZE}
const painted = [];
window.STATE_READY.then(async state => {{
  clearTimeout(watchdog);
  const before = summary(state);
  // The rows the reader is looking at, by identity: the merge must update
  // these and not replace them.
  painted.push(...state.inventory);
  await window.STATE_DERIVED_READY;
  console.log(JSON.stringify({{
    resolved: true,
    timeout: false,
    requested: requested.slice(),
    before,
    after: summary(window.STATE),
    same_rows: window.STATE.inventory.every((row, index) => row === painted[index]),
    row_count: window.STATE.inventory.length,
    active_sprint_id: window.STATE.active_sprint_id,
    sprints: window.STATE.sprints.map(sprint => sprint.id),
    milestones: window.STATE.milestones.map(milestone => milestone.id),
    source_format: window.STATE.source_format,
    plans_keys: Object.keys(window.STATE.plans),
  }}));
}});
"""
    observed = _run_node(_INDEX_ANSWERS + _DISCOVERY_ANSWERS, body)

    assert observed["resolved"] is True, observed
    before, after = observed["before"], observed["after"]

    assert observed["requested"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT]
    # The first paint had no derived state; the merge brought it.
    assert before["ready_set"] == {}
    assert after["ready_set"] == DISCOVERY["ready_set"]

    assert after["keys"] == INDEX_ORDER == before["keys"], (
        "the merge changed the order of the rows on screen: "
        f"{before['keys']} -> {after['keys']}"
    )
    assert observed["same_rows"] is True, (
        "the merge replaced the row objects instead of updating them: "
        f"{observed['row_count']} rows, same_rows={observed['same_rows']}"
    )
    assert observed["row_count"] == len(INDEX_ROWS)

    assert after["effective_status"] == ["blocked", "ready", "recorded", "draft"]
    assert after["effective_status"] != before["effective_status"]
    assert after["impl"] == [0.4, 0.9, None, None]
    assert after["titles"] == before["titles"]

    # The payload-level derived blocks arrive with the rows.
    assert observed["active_sprint_id"] == "focus"
    assert observed["sprints"] == ["focus"]
    assert observed["milestones"] == ["m1"]
    assert observed["source_format"] == "distributed"
    assert observed["plans_keys"] == INDEX_ORDER


def test_an_unavailable_index_falls_back_to_discovery() -> None:
    """404 and a refused request both leave the page painted from discovery."""

    body = f"""
{_SUMMARIZE}
window.STATE_READY.then(state => {{
  clearTimeout(watchdog);
  console.log(JSON.stringify({{
    resolved: true,
    timeout: false,
    requested: requested.slice(),
    keys: summary(state).keys,
    ready_set: state.ready_set,
    effective_status: summary(state).effective_status,
  }}));
}});
"""
    for behaviour in (_INDEX_IS_ABSENT, _INDEX_REFUSED):
        observed = _run_node(behaviour + _DISCOVERY_ANSWERS, body)
        assert observed["resolved"] is True, observed
        assert observed["requested"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT], observed
        # The discovery path is the old one, so its rows and derived state are
        # in the first paint and nothing downstream had to wait for a merge.
        assert observed["keys"] == ["alpha", "beta", "evidence:gate"], observed
        assert observed["ready_set"] == DISCOVERY["ready_set"], observed
        assert observed["effective_status"] == ["blocked", "ready", "recorded"], (
            observed
        )
