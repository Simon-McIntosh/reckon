"""The change stream pushes the rows that moved, and the open page patches them.

``/_changes/<project>`` used to report a change as a bare content digest, and
an open page answered every event by fetching the whole discovery payload
again. The stream now carries the index rows that changed, were added or were
removed, taken from the metadata index diff, and the loader patches them into
``window.STATE`` in place — a row the page never carried is held as an arrival
— while the derived state follows at most once per settle window, in the
background. A server that predates the row payload still works: an event
carrying only a digest answers with the refetch the loader always made.

The first cases run the real server over a temporary project; the rest feed
constructed events to the real ``docs/ui/state-loader.js`` under ``node`` with
a recording ``fetch``, so the assertions are about the requests the loader
actually made and the state it actually patched.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from reckon import metadata_index, serve

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "docs" / "ui" / "state-loader.js"

PROJECT = "sample"
INDEX_ENDPOINT = f"/_index/{PROJECT}"
DISCOVERY_ENDPOINT = f"/_discover/{PROJECT}"

#: The time the change stream must answer within: a rewrite is reported with
#: its row inside it.
PUSH_BUDGET_S = 2.0

#: The loader's own settle window, read from the source so the cases that wait
#: on it cannot drift from the constant they are testing.
_CHANGE_SETTLE_MS = int(
    re.search(r"const CHANGE_SETTLE_MS = (\d+);", LOADER.read_text(encoding="utf-8"))[1]
)
_SETTLE_WAIT_MS = _CHANGE_SETTLE_MS + 400


# ─── The served stream ────────────────────────────────────────────────────


def _plan_page(slug: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<title>{title}</title></head><body><main class="plan-doc"></main></body></html>
"""


def _rewrite(path: Path, slug: str, title: str) -> None:
    """Rewrite one plan page so its stat identity moves with it."""

    previous = path.stat().st_mtime_ns
    path.write_text(_plan_page(slug, title), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1)))


def _get(port: int, path: str) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class _ChangeStream:
    """Read ``/_changes/<project>`` frames, the event and its data together."""

    def __init__(self, port: int, project: str) -> None:
        self._connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self._connection.request("GET", f"/_changes/{project}")
        self._response = self._connection.getresponse()

    def next_event(self) -> tuple[str, dict | None]:
        event = ""
        data: str | None = None
        while True:
            line = self._response.readline().decode("utf-8")
            if not line:
                raise AssertionError("the change stream closed before an event")
            line = line.rstrip("\r\n")
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
            elif not line and event:
                return event, (json.loads(data) if data is not None else None)

    def close(self) -> None:
        self._connection.close()


@pytest.fixture
def served_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docs = tmp_path / "docs"
    (docs / "plans").mkdir(parents=True)
    (docs / "plans" / "alpha.html").write_text(
        _plan_page("alpha", "Alpha"), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "load_mounts", lambda: {PROJECT: docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    metadata_index.clear()
    serve._DISC_CACHE.clear()
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield docs, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_title_rewrite_pushes_that_row_within_two_seconds(served_project) -> None:
    """The next event after a rewrite carries that row and nothing else."""

    docs, server = served_project
    target = docs / "plans" / "alpha.html"
    stream = _ChangeStream(server.server_port, PROJECT)
    try:
        assert stream.next_event()[0] == "ready"
        # A page painting from the index has already read the row list; the
        # push is diffed against what such a page holds.
        assert _get(server.server_port, INDEX_ENDPOINT)[0] == 200

        started = time.monotonic()
        _rewrite(target, "alpha", "Alpha renamed")
        event, payload = stream.next_event()
        elapsed = time.monotonic() - started
    finally:
        stream.close()

    assert event == "change", payload
    assert payload is not None and "rows" in payload, (
        f"the change event carried no rows: {payload}"
    )
    assert elapsed < PUSH_BUDGET_S, (
        f"the pushed row arrived {elapsed:.2f} s after the rewrite"
    )

    status, rows = _get(server.server_port, INDEX_ENDPOINT)
    assert status == 200
    served = next(row for row in rows if row["slug"] == "alpha")
    assert served["title"] == "Alpha renamed"
    # Exactly the row that moved, shaped as the endpoint serves it.
    assert payload["rows"] == {"changed": [served], "added": [], "removed": []}


def test_a_new_file_arrives_as_added_and_a_delete_as_removed(served_project) -> None:
    """The diff names an arrival as an addition and a departure as a removal."""

    docs, server = served_project
    stream = _ChangeStream(server.server_port, PROJECT)
    try:
        assert stream.next_event()[0] == "ready"

        arrival = docs / "plans" / "beta.html"
        arrival.write_text(_plan_page("beta", "Beta"), encoding="utf-8")
        event, payload = stream.next_event()
        assert event == "change", payload
        assert [row["slug"] for row in payload["rows"]["added"]] == ["beta"]
        assert payload["rows"]["changed"] == []
        assert payload["rows"]["removed"] == []

        arrival.unlink()
        event, payload = stream.next_event()
        assert event == "change", payload
        assert [row["slug"] for row in payload["rows"]["removed"]] == ["beta"]
        assert payload["rows"]["added"] == []
        assert payload["rows"]["changed"] == []
    finally:
        stream.close()


# ─── The loader patching a pushed event (real loader under node) ──────────

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

#: The derived payload that follows the first paint: effective status and the
#: ready set, which only the server computes.
DISCOVERY = {
    "active_sprint_id": "focus",
    "sprints": [{"id": "focus", "status": "active", "items": []}],
    "ready_set": {"ready": ["alpha"], "total": 1},
    "inventory": [
        {
            "slug": "alpha",
            "type": "plan",
            "title": "Alpha",
            "status": "active",
            "effective_status": "blocked",
            "blockers": 1,
            "created": 100,
            "edited": "2026-01-01T00:00:00",
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

#: The rewritten alpha row as the stream pushes it.
CHANGED_ROW = {
    "slug": "alpha",
    "href": "plans/alpha",
    "type": "plan",
    "title": "Alpha renamed",
    "status": "active",
    "sprint": "focus",
    "archived": "",
    "created": 100,
    "edited": "2026-01-05T00:00:00",
    "width": None,
    "height": None,
}

#: A row the index did not carry before, and the row of a document that left.
ADDED_ROW = {
    "slug": "late",
    "href": "evidence/late",
    "type": "evidence",
    "title": "Late",
    "status": "",
    "sprint": None,
    "archived": "",
    "created": 70,
    "edited": "2026-01-06T00:00:00",
    "width": None,
    "height": None,
}
GATE_ROW = INDEX_ROWS[1]

CHANGED_EVENT = {
    "content_digest": "digest-1",
    "rows": {"changed": [CHANGED_ROW], "added": [], "removed": []},
}
ARRIVAL_EVENT = {
    "content_digest": "digest-2",
    "rows": {"changed": [], "added": [ADDED_ROW], "removed": [GATE_ROW]},
}
#: The document that arrived above leaves again before the reader revealed it.
REMOVAL_EVENT = {
    "content_digest": "digest-3",
    "rows": {"changed": [], "added": [], "removed": [ADDED_ROW]},
}

_HARNESS = """
const fs = require("fs");
const PROJECT = __PROJECT__;
const LOADER_PATH = __LOADER__;
const SETTLE_WAIT_MS = __SETTLE_WAIT__;
const DATA = __DATA__;
global.window = { location: { pathname: "/" + PROJECT + "/" } };
global.document = { querySelector: () => ({ content: PROJECT }) };
const requested = [];
const ok = (payload) => ({ ok: true, status: 200, json: async () => payload });
global.fetch = async (url) => {
  requested.push(url);
  if (url === "/_index/" + PROJECT) return ok(DATA.index);
  if (url === "/_discover/" + PROJECT) return ok(DATA.discovery);
  throw new Error("unexpected fetch " + url);
};
class TestEventSource {
  constructor(url) { this.url = url; this.listeners = {}; }
  addEventListener(event, listener) { (this.listeners[event] ||= []).push(listener); }
  close() {}
  emit(event, payload) {
    for (const listener of (this.listeners[event] || [])) {
      listener(payload === undefined ? undefined : { data: JSON.stringify(payload) });
    }
  }
}
global.EventSource = TestEventSource;
eval(fs.readFileSync(LOADER_PATH, "utf8"));
const summary = state => ({
  keys: state.inventory.map(row => row.nav_key),
  titles: state.inventory.map(row => row.title),
  effective_status: state.inventory.map(row => row.effective_status),
  pending: (state.arrival?.pending || []).map(row => row.nav_key),
  pending_titles: (state.arrival?.pending || []).map(row => row.title),
  receipt: state.arrival?.receipt,
  ready_set: state.ready_set,
  plans_keys: Object.keys(state.plans),
});
const watchdog = setTimeout(() => {
  console.log(JSON.stringify({ resolved: false, timeout: true }));
  process.exit(0);
}, 10000);
__BODY__
"""


def _run_node(body: str) -> dict:
    """Run the real loader under node with a recording fetch and a stub stream."""

    script = (
        _HARNESS.replace("__PROJECT__", json.dumps(PROJECT))
        .replace("__LOADER__", json.dumps(str(LOADER)))
        .replace("__SETTLE_WAIT__", str(_SETTLE_WAIT_MS))
        .replace(
            "__DATA__",
            json.dumps(
                {
                    "index": INDEX_ROWS,
                    "discovery": DISCOVERY,
                    "changed_event": CHANGED_EVENT,
                    "arrival_event": ARRIVAL_EVENT,
                    "removal_event": REMOVAL_EVENT,
                }
            ),
        )
        .replace("__BODY__", body)
    )
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


def test_a_pushed_row_patches_in_place_without_a_request() -> None:
    """The event's row lands on the state; applying it costs no fetch."""

    body = """
window.STATE_READY.then(async () => {
  await window.STATE_DERIVED_READY;
  let refetches = 0;
  const stream = window.watchProjectStateChanges(async () => { refetches += 1; });
  const alpha = window.STATE.inventory.find(row => row.nav_key === "alpha");
  const before = requested.slice();
  stream.emit("change", DATA.changed_event);
  const pushed = {
    resolved: true,
    title: alpha.title,
    effective_status: alpha.effective_status,
    same_object: window.STATE.inventory.find(row => row.nav_key === "alpha") === alpha,
    requested_immediately: requested.slice(),
    refetches_immediately: refetches,
  };
  await new Promise(resolve => setTimeout(resolve, SETTLE_WAIT_MS));
  pushed.summary = summary(window.STATE);
  pushed.requested_after_settle = requested.slice();
  pushed.refetches_after_settle = refetches;
  clearTimeout(watchdog);
  console.log(JSON.stringify(pushed));
});
"""
    observed = _run_node(body)

    assert observed["resolved"] is True, observed
    assert observed["title"] == "Alpha renamed", (
        f"the pushed row did not reach window.STATE: {observed['title']}"
    )
    assert observed["same_object"] is True, "the patch replaced the row object"
    # The derived fields on the row were computed in Python; the patch left
    # them alone rather than defaulting them locally.
    assert observed["effective_status"] == "blocked", observed["effective_status"]
    assert observed["summary"]["effective_status"][0] == "blocked"

    # Applying the push made no request: /_index and /_discover are exactly the
    # first paint's own fetches, and the refetch is deferred, not immediate.
    assert observed["requested_immediately"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT], (
        f"applying the pushed row made a request: {observed['requested_immediately']}"
    )
    assert observed["refetches_immediately"] == 0

    # The derived refresh follows, once, in the background.
    assert observed["refetches_after_settle"] == 1
    assert observed["requested_after_settle"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT], (
        "the background refetch did not go through the caller's own refresh: "
        f"{observed['requested_after_settle']}"
    )
    paint = observed["summary"]
    assert paint["keys"] == ["alpha", "evidence:gate", "figure:plot.png"]
    assert paint["titles"] == ["Alpha renamed", "Gate", "Plot"]
    assert paint["pending"] == []
    assert paint["ready_set"] == DISCOVERY["ready_set"]


def test_an_added_row_is_held_as_an_arrival_and_a_removed_row_leaves() -> None:
    """A row the page never carried is an arrival; a departed row is dropped."""

    body = """
window.STATE_READY.then(async () => {
  await window.STATE_DERIVED_READY;
  const stream = window.watchProjectStateChanges(async () => {});
  stream.emit("change", DATA.arrival_event);
  clearTimeout(watchdog);
  console.log(JSON.stringify({
    resolved: true,
    summary: summary(window.STATE),
    requested_tail: requested.slice(2),
  }));
});
"""
    observed = _run_node(body)

    assert observed["resolved"] is True, observed
    summary = observed["summary"]
    # The removed row left the inventory, and the rows around it kept order.
    assert summary["keys"] == ["alpha", "figure:plot.png"], summary["keys"]
    assert "evidence:gate" not in summary["plans_keys"]
    # The row the page never carried is held as an arrival rather than
    # inserted into the list on screen.
    assert summary["pending"] == ["evidence:late"]
    assert summary["pending_titles"] == ["Late"]
    assert summary["receipt"] == "1 new"
    assert "evidence:late" not in summary["keys"]
    assert observed["requested_tail"] == [], (
        f"the push made a request: {observed['requested_tail']}"
    )


def test_a_digest_only_event_falls_back_to_the_refetch() -> None:
    """A server that does not push rows still gets the refetch it always did."""

    body = """
window.STATE_READY.then(async () => {
  await window.STATE_DERIVED_READY;
  const before = requested.slice();
  const stream = window.watchProjectStateChanges(async () => {
    await window.revalidateProjectState();
  });
  stream.emit("change", { content_digest: "legacy-digest" });
  await new Promise(resolve => setTimeout(resolve, SETTLE_WAIT_MS));
  clearTimeout(watchdog);
  console.log(JSON.stringify({ resolved: true, before, after: requested.slice() }));
});
"""
    observed = _run_node(body)

    assert observed["resolved"] is True, observed
    assert observed["before"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT]
    assert observed["after"] == [
        INDEX_ENDPOINT,
        DISCOVERY_ENDPOINT,
        INDEX_ENDPOINT,
        DISCOVERY_ENDPOINT,
    ], (
        "a digest-only event did not fall back to the refetch it does today: "
        f"{observed['after']}"
    )


def test_change_events_cost_one_background_refetch_per_settle_window() -> None:
    """A burst of events is one derived refresh, however many rows it moves."""

    body = """
window.STATE_READY.then(async () => {
  await window.STATE_DERIVED_READY;
  let refetches = 0;
  const stream = window.watchProjectStateChanges(async () => { refetches += 1; });
  for (let index = 0; index < 4; index++) {
    stream.emit("change", DATA.changed_event);
  }
  const immediately = refetches;
  await new Promise(resolve => setTimeout(resolve, SETTLE_WAIT_MS));
  clearTimeout(watchdog);
  console.log(JSON.stringify({
    resolved: true,
    immediately,
    refetches,
    title: window.STATE.inventory.find(row => row.nav_key === "alpha").title,
  }));
});
"""
    observed = _run_node(body)

    assert observed["resolved"] is True, observed
    assert observed["title"] == "Alpha renamed"
    assert observed["immediately"] == 0, "the refetch ran inside the event"
    assert observed["refetches"] == 1, (
        f"four events in one settle window cost {observed['refetches']} refetches"
    )


def test_a_mixed_burst_costs_one_background_refetch() -> None:
    """Pushed rows and digest-only events share one settle window."""

    body = """
window.STATE_READY.then(async () => {
  await window.STATE_DERIVED_READY;
  let refetches = 0;
  const stream = window.watchProjectStateChanges(async () => { refetches += 1; });
  stream.emit("change", DATA.changed_event);
  stream.emit("change", { content_digest: "legacy-digest" });
  stream.emit("change", DATA.changed_event);
  stream.emit("change", { content_digest: "legacy-digest-2" });
  const immediately = refetches;
  await new Promise(resolve => setTimeout(resolve, SETTLE_WAIT_MS));
  clearTimeout(watchdog);
  console.log(JSON.stringify({
    resolved: true,
    immediately,
    refetches,
    title: window.STATE.inventory.find(row => row.nav_key === "alpha").title,
  }));
});
"""
    observed = _run_node(body)

    assert observed["resolved"] is True, observed
    assert observed["title"] == "Alpha renamed"
    assert observed["immediately"] == 0, "the refetch ran inside the event"
    assert observed["refetches"] == 1, (
        "a burst mixing pushed rows with digest-only events cost "
        f"{observed['refetches']} refetches"
    )


def test_a_row_removed_before_its_reveal_leaves_the_arrival_count() -> None:
    """A held arrival that is deleted is pruned from the arrival banner."""

    body = """
window.STATE_READY.then(async () => {
  await window.STATE_DERIVED_READY;
  const stream = window.watchProjectStateChanges(async () => {});
  stream.emit("change", DATA.arrival_event);
  const held = {
    pending: (window.STATE.arrival?.pending || []).map(row => row.nav_key),
    receipt: window.STATE.arrival?.receipt,
  };
  stream.emit("change", DATA.removal_event);
  clearTimeout(watchdog);
  console.log(JSON.stringify({
    resolved: true,
    held,
    pending: (window.STATE.arrival?.pending || []).map(row => row.nav_key),
    total: window.STATE.arrival?.total,
    receipt: window.STATE.arrival?.receipt,
    keys: window.STATE.inventory.map(row => row.nav_key),
  }));
});
"""
    observed = _run_node(body)

    assert observed["resolved"] is True, observed
    # Positive control: the row really was held before it was deleted.
    assert observed["held"]["pending"] == ["evidence:late"], observed["held"]
    assert observed["held"]["receipt"] == "1 new"
    assert observed["pending"] == [], (
        f"a deleted arrival stayed in the count: {observed['pending']}"
    )
    assert observed["total"] == 0
    assert observed["receipt"] == "live"
    assert observed["keys"] == ["alpha", "figure:plot.png"]
