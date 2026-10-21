"""The page paints from the index, and derived state arrives afterwards.

The served loader asks for the index first — one row per document and figure,
carrying each plan's progress figure (impl) and nothing else derived — and
resolves the first paint on it, so every progress bar is drawn without waiting
for discovery. The discovery payload, which carries the remaining derived
fields and the ready set, is fetched
afterwards in the background and merged into the rows already on screen,
without re-sorting them. An index that is unavailable must not cost the page
its rows: the loader then takes the discovery path it used before.

Each loader case runs the real ``docs/ui/state-loader.js`` under ``node`` with
a recording ``fetch``, so the assertions are about the requests the loader
actually made and the state it actually assembled. A case whose readiness
never comes fails on its timeout rather than hanging the suite.

The last case runs the real server: a page paints from the index, so the
change stream that reports a tree must leave that tree's index rows fresh, or
the reader revalidates against a row list that predates the change they were
just told about.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from reckon import metadata_index, serve
from tests.spa_browser_harness import installed_browser_or_skip, served_spa

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
        "impl": 0.4,
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
        "impl": 0.9,
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

#: The schedule a discovery serves, and the one a later discovery serves: the
#: far bound and the reference move, so a schedule kept through a revalidation
#: and one replaced by the merge are distinguishable by value and not only by
#: object identity.
SCHEDULE_FIRST = {
    "reference": "2026-01-10T00:00:00+00:00",
    "low": -4,
    "high": 8,
    "lane_count": 1,
    "item_count": 1,
    "bars": [
        {
            "nav_key": "alpha",
            "lane": 0,
            "start": -2,
            "end": 4,
            "split": 0,
            "state": "predicted",
            "wall_hours": 6,
        }
    ],
}

# The same schedule, served later, with its far bound and its reference moved.
SCHEDULE_REPLACEMENT = {
    **SCHEDULE_FIRST,
    "reference": "2026-01-10T00:05:00+00:00",
    "high": 12,
}

#: What ``/_schedule/<project>`` answers in the cases below: the same plan, now
#: drawn in flight and anchored at a later instant, so a schedule that came from
#: the route is distinguishable by value from either one a discovery served.
SCHEDULE_FROM_ROUTE = {
    **SCHEDULE_FIRST,
    "reference": "2026-01-10T00:30:00+00:00",
    "high": 10,
    "bars": [
        {
            "nav_key": "alpha",
            "lane": 0,
            "start": -2,
            "end": 6,
            "split": 0,
            "state": "in-flight",
            "wall_hours": 8,
        }
    ],
}

#: The keys a revalidation must keep, written out as literals rather than read
#: back off the loader, so a key dropped from its list fails the case.
DERIVED_STATE_KEYS = [
    "sprints",
    "active_sprint_id",
    "active_sprints",
    "active_sprint_conflict",
    "sprint",
    "ready_set",
    "endpoints",
    "source_format",
    "resource_versions",
    "blockers",
    "timeline",
    "schedule",
    "milestones",
]

#: The same rows plus what the index deliberately does not carry: the effective
#: status each row derives once blockers are known, and the ready set. The plan
#: rows repeat impl, which both payloads carry, so the merge leaves the bars the
#: first paint drew where they were.
DISCOVERY = {
    "source_format": "distributed",
    "active_sprint_id": "focus",
    "sprints": [{"id": "focus", "status": "active", "items": []}],
    "milestones": [{"id": "m1", "title": "M1"}],
    "north_stars": [{"id": "north"}],
    "timeline": [{"id": "t1"}],
    "blockers": [{"slug": "alpha"}],
    "endpoints": [{"project": "sample", "slug": "alpha"}],
    "schedule": SCHEDULE_FIRST,
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

#: The same payload with one row the index never carried: it must land as an
#: arrival rather than be inserted into the list already on screen.
DISCOVERY_WITH_ARRIVAL = {
    **DISCOVERY,
    "inventory": [
        *DISCOVERY["inventory"],
        {
            "slug": "late",
            "type": "evidence",
            "title": "Late",
            "status": "recorded",
            "effective_status": "recorded",
            "created": 60,
            "edited": "2026-01-06T00:00:00",
        },
    ],
}

#: Discovery with its schedule withheld: the read could not build one, so it
#: serves null with the failure beside it and the schedule on screen is kept.
DISCOVERY_WITHOUT_SCHEDULE = {
    **DISCOVERY,
    "schedule": None,
    "schedule_error": "RuntimeError: crew rows unavailable",
}

#: The schedule a revalidation's discovery serves: one bar more than the first
#: load drew, so a flow that comes to show it can only have rendered after the
#: merge. The served-page case below holds the checks route and the crew poll
#: unanswered, which are the other two routes a repaint could arrive on, so the
#: count it asserts is the merge's own event reaching the shell.
SCHEDULE_TWO_BARS = {
    **SCHEDULE_FIRST,
    "reference": "2026-01-10T00:10:00+00:00",
    "item_count": 2,
    "bars": [
        *SCHEDULE_FIRST["bars"],
        {
            "nav_key": "beta",
            "lane": 0,
            "start": 0,
            "end": 5,
            "split": 2,
            "state": "recorded",
            "wall_hours": 5,
        },
    ],
}

#: The same payload the first load answers, with that schedule in it.
DISCOVERY_REPLACEMENT = {**DISCOVERY, "schedule": SCHEDULE_TWO_BARS}

#: The order the index listed the rows in, which the merge must not change.
INDEX_ORDER = ["alpha", "beta", "evidence:gate", "figure:plot.png"]

_HARNESS = """
const fs = require("fs");
const PROJECT = __PROJECT__;
const LOADER_PATH = __LOADER__;
const READY_TIMEOUT_MS = __TIMEOUT__;
const DATA = __DATA__;
const dispatched = [];
global.CustomEvent = global.CustomEvent || class {
  constructor(type, init) { this.type = type; Object.assign(this, init || {}); }
};
global.window = {
  location: { pathname: "/" + PROJECT + "/" },
  dispatchEvent: event => { dispatched.push(event.type); return true; },
};
global.document = { querySelector: () => ({ content: PROJECT }) };
// The change stream, driven by hand: a case pushes the exact event it is about
// through the listener the loader registered, so nothing here depends on a
// server or on timing.
global.EventSource = class {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    EventSource.instances.push(this);
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  emit(type, payload) {
    for (const handler of this.listeners[type] || []) {
      handler({ data: JSON.stringify(payload) });
    }
  }
  close() { this.closed = true; }
};
EventSource.instances = [];
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

# The project's schedule, from its own route. The answer is the whole one the
# route gives, so a case reading it reads what the server would serve.
_SCHEDULE_ANSWERS = """
  if (url === "/_index/sample") {
    requested.push(url);
    return ok(DATA.index);
  }
  if (url === "/_discover/sample") {
    requested.push(url);
    return ok(DATA.discovery);
  }
  if (url === "/_schedule/sample") {
    requested.push(url);
    return ok({ project: "sample", schedule: DATA.schedule_route });
  }
"""

# /_discover answers after a delay, and answers a different payload on the
# second call. The delay is what puts a window between the index landing and
# the merge, which is the window the revalidation must keep its blocks through;
# the replacement payload is what makes the merge distinguishable from the
# blocks that were kept.
_DISCOVERY_DELAYED_THEN_REPLACED = """
  if (url === "/_index/sample") {
    requested.push(url);
    return ok(DATA.index);
  }
  if (url === "/_discover/sample") {
    requested.push(url);
    const seen = requested.filter(item => item === "/_discover/sample").length;
    const payload = seen > 1 ? DATA.discovery_replacement : DATA.discovery;
    return new Promise(resolve => setTimeout(() => resolve(ok(payload)), 120));
  }
"""

_DISCOVERY_ANSWERS_WITH_ARRIVAL = """
  if (url === "/_discover/sample") {
    requested.push(url);
    return ok(DATA.discovery_arrival);
  }
"""

# The same delay, with the second discovery's schedule withheld: the read could
# not build one, so it serves null with the failure beside it.
_DISCOVERY_DELAYED_THEN_WITHHELD = """
  if (url === "/_index/sample") {
    requested.push(url);
    return ok(DATA.index);
  }
  if (url === "/_discover/sample") {
    requested.push(url);
    const seen = requested.filter(item => item === "/_discover/sample").length;
    const payload = seen > 1 ? DATA.discovery_without_schedule : DATA.discovery;
    return new Promise(resolve => setTimeout(() => resolve(ok(payload)), 120));
  }
"""

# /_checks is held unanswered: the render that follows the merge cannot be the
# one the checks path used to deliver, so the merge is what asked for it.
_CHECKS_NEVER = """
  if (url === "/_checks/sample") {
    requested.push(url);
    return never();
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


def _run_node(behaviour: str, body: str, timeout_ms: int = READY_TIMEOUT_MS) -> dict:
    """Run the real loader under node against a recording fetch."""

    script = (
        _HARNESS.replace("__PROJECT__", json.dumps(PROJECT))
        .replace("__LOADER__", json.dumps(str(LOADER)))
        .replace("__TIMEOUT__", str(timeout_ms))
        .replace(
            "__DATA__",
            json.dumps(
                {
                    "index": INDEX_ROWS,
                    "discovery": DISCOVERY,
                    "discovery_arrival": DISCOVERY_WITH_ARRIVAL,
                    "discovery_replacement": {
                        **DISCOVERY,
                        "schedule": SCHEDULE_REPLACEMENT,
                    },
                    "discovery_without_schedule": DISCOVERY_WITHOUT_SCHEDULE,
                    "schedule_route": SCHEDULE_FROM_ROUTE,
                }
            ),
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
    # The progress bars are drawn from the index alone: each plan row carries
    # its impl, while the evidence and figure rows carry none. This is the
    # first-paint contract — the bars do not wait for the discovery merge.
    assert paint["impl"][:2] == [0.4, 0.9]
    # Written as a literal, not read back off INDEX_ROWS: an assertion whose two
    # sides are both derived from the fixture the test itself hands the loader
    # moves with the fixture and holds whatever the loader does. The literal
    # above and this one agree, and both go red if the first paint drops impl.
    assert paint["impl"][2:] == [None, None]


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


# ─── The served index, after a change the page's own stream reported ──────


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


def _write(title: str, path: Path) -> None:
    """Rewrite one plan page so its stat identity moves with it."""

    previous = path.stat().st_mtime_ns
    path.write_text(_plan_page("alpha", title), encoding="utf-8")
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


def _live_threads() -> set[int]:
    """The idents of the threads running at the moment of the call."""

    return {thread.ident for thread in threading.enumerate() if thread.ident is not None}


#: How long a handler thread is given to leave once its stream is closed. A
#: handler that saw EOF exits in milliseconds; one still in its change-stream
#: wait after this window is the leak the two server cases guard against.
_HANDLER_EXIT_GRACE_S = 2.0


def _handlers_left(before: set[int]) -> set[int]:
    """The threads still alive once the grace window has passed.

    Read before ``server_close()``, never after. That call joins the non-daemon
    handler threads (``server.daemon_threads = False`` in each case), so a
    handler still in its change-stream wait blocks the join there instead of
    reddening an assertion made after it — and by the time that join returns,
    the leaked handler has already exited, so a roster read placed after it
    looks clean either way.
    """

    deadline = time.monotonic() + _HANDLER_EXIT_GRACE_S
    extra = _live_threads() - before
    while extra and time.monotonic() < deadline:
        time.sleep(0.05)
        extra = _live_threads() - before
    return extra


def _title(rows: list[dict], slug: str) -> str:
    return next(row["title"] for row in rows if row["slug"] == slug)


class _ChangeStream:
    """Read ``/_changes/<project>`` frames, one event at a time."""

    def __init__(self, port: int, project: str) -> None:
        self._connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self._connection.request("GET", f"/_changes/{project}")
        self._response = self._connection.getresponse()

    def next_event(self) -> str:
        event = ""
        while True:
            line = self._response.readline().decode("utf-8")
            if not line:
                raise AssertionError("the change stream closed before an event")
            line = line.rstrip("\r\n")
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif not line and event:
                return event

    def close(self) -> None:
        # The response holds a makefile over the socket, so closing the
        # connection alone leaves the file descriptor open: the server's
        # handler never sees EOF and blocks in its change-stream wait for the
        # rest of the process, reaching whatever test runs next.
        self._response.close()
        self._connection.close()


def test_a_reported_change_refreshes_the_served_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream that reports a tree change drops that tree's index rows.

    A page paints from ``/_index/<project>``. Once the reader's own stream has
    told it the tree moved, the next index read must not hand back the row as
    it was: every watch that reports a tree drops the same views of it.
    """

    docs = tmp_path / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    target = plans / "alpha.html"
    target.write_text(_plan_page("alpha", "Alpha"), encoding="utf-8")

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "load_mounts", lambda: {PROJECT: docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    metadata_index.clear()
    serve._DISC_CACHE.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    # A ThreadingHTTPServer runs its handler threads as daemons, which the
    # standard library's server_close() does not join. Left daemonic, a stream
    # handler outlives the test and can call into whatever the next test has
    # patched; non-daemonic handler threads are joined when the server closes.
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Taken after the accept loop starts, so the only threads this reads as new
    # are the ones a request spawned -- the handler under guard.
    threads_before = _live_threads()
    stream = _ChangeStream(server.server_port, PROJECT)
    try:
        assert stream.next_event() == "ready"
        # Read the endpoint first, so the assertion below is about the
        # invalidation and not about a first, cold read.
        status, rows = _get(server.server_port, INDEX_ENDPOINT)
        assert status == 200
        assert _title(rows, "alpha") == "Alpha"

        _write("Alpha renamed", target)
        assert stream.next_event() == "change"

        status, rows = _get(server.server_port, INDEX_ENDPOINT)
    finally:
        stream.close()
        # Read the roster here, while the handlers are still unjoined, and let
        # the grace window pass inside _handlers_left.
        leaked = _handlers_left(threads_before)
        server.shutdown()
        if leaked:
            # server_close() joins the non-daemon handler threads, so a leaked
            # handler blocks it for the rest of the run: the listening socket
            # is closed without that join and the guard below reddens instead.
            server.socket.close()
        else:
            server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert _title(rows, "alpha") == "Alpha renamed"

    # A change-stream handler thread must end with the test that opened the
    # stream. The control reverts the response close in _ChangeStream.close,
    # which is the leak this guard exists for.
    assert leaked == set(), (
        "a change-stream handler thread outlived its test: the stream's close "
        "left the socket the handler is waiting on open, so the server's "
        "handler never saw EOF and is still in its change-stream wait"
    )


def test_two_change_streams_recompute_discovery_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One reported change costs one rediscovery however many pages watch it.

    Every open stream observes the same filesystem event, so the recomputation
    the first one pays for is what the others read instead of each repeating
    the scan.
    """

    docs = tmp_path / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    target = plans / "alpha.html"
    target.write_text(_plan_page("alpha", "Alpha"), encoding="utf-8")

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "load_mounts", lambda: {PROJECT: docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    metadata_index.clear()
    serve._DISC_CACHE.clear()

    scans: list[str] = []
    uncached = serve._discover_plans_uncached

    def counted(docs_dir, project, state_root, sig, cache_key):
        scans.append(str(docs_dir))
        return uncached(docs_dir, project, state_root, sig, cache_key)

    monkeypatch.setattr(serve, "_discover_plans_uncached", counted)

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    # A ThreadingHTTPServer runs its handler threads as daemons, which the
    # standard library's server_close() does not join. Left daemonic, a stream
    # handler outlives the test and can call into whatever the next test has
    # patched; non-daemonic handler threads are joined when the server closes.
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Taken after the accept loop starts, so the only threads this reads as new
    # are the ones a request spawned -- the handler under guard.
    threads_before = _live_threads()
    streams = [_ChangeStream(server.server_port, PROJECT) for _ in range(2)]
    try:
        for stream in streams:
            assert stream.next_event() == "ready"
        baseline = len(scans)

        _write("Alpha renamed", target)
        for stream in streams:
            assert stream.next_event() == "change"
    finally:
        for stream in streams:
            stream.close()
        # Read the roster here, while the handlers are still unjoined, and let
        # the grace window pass inside _handlers_left.
        leaked = _handlers_left(threads_before)
        server.shutdown()
        if leaked:
            # server_close() joins the non-daemon handler threads, so a leaked
            # handler blocks it for the rest of the run: the listening socket
            # is closed without that join and the guard below reddens instead.
            server.socket.close()
        else:
            server.server_close()
        thread.join(timeout=5)

    # The cold read scanned the tree; the change must add exactly one more.
    assert baseline >= 1, "the tree was never scanned, so the count proves nothing"
    assert len(scans) - baseline == 1, (
        f"{len(scans) - baseline} rediscoveries for one reported change "
        "across two open streams"
    )

    # A change-stream handler thread must end with the test that opened the
    # stream. The control reverts the response close in _ChangeStream.close,
    # which is the leak this guard exists for.
    assert leaked == set(), (
        "a change-stream handler thread outlived its test: the stream's close "
        "left the socket the handler is waiting on open, so the server's "
        "handler never saw EOF and is still in its change-stream wait"
    )


def test_a_report_keeps_a_discovery_newer_than_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report drops what predates it, not what another watcher just rebuilt.

    Two watchers of one tree report the same filesystem event; the second
    observed it before the first rebuilt the rows, so the rebuild must survive
    the second's report — while the index rows go on every report, so a page
    painting from them never reads a list that predates the change.
    """

    docs = tmp_path / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    (plans / "alpha.html").write_text(_plan_page("alpha", "Alpha"), encoding="utf-8")

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "load_mounts", lambda: {PROJECT: docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    metadata_index.clear()
    serve._DISC_CACHE.clear()

    scans: list[str] = []
    uncached = serve._discover_plans_uncached

    def counted(docs_dir, project, state_root, sig, cache_key):
        scans.append(str(docs_dir))
        return uncached(docs_dir, project, state_root, sig, cache_key)

    monkeypatch.setattr(serve, "_discover_plans_uncached", counted)

    builds: list[str] = []
    built = metadata_index.build_index

    def counted_build(docs_dir, project, **kwargs):
        builds.append(str(docs_dir))
        return built(docs_dir, project, **kwargs)

    monkeypatch.setattr(metadata_index, "build_index", counted_build)

    assert serve.discover_plans(docs, PROJECT, None)
    assert len(scans) == 1, "the cold read never scanned, so the count proves nothing"
    assert metadata_index.index_rows(docs, PROJECT)
    assert len(builds) == 1

    # One watcher observes the change and reports it; the rows and the cached
    # discovery that predate it go, and the next read rebuilds them.
    observed_at = time.monotonic()
    _write("Alpha renamed", plans / "alpha.html")
    serve._invalidate_tree_views(docs, changed_at=observed_at)
    rebuilt = serve.discover_plans(docs, PROJECT, None)

    assert len(scans) == 2
    assert _title(rebuilt["inventory"], "alpha") == "Alpha renamed"

    # A second watcher reports the same change, observed at the same moment:
    # the rebuild that already answers it must survive its report.
    serve._invalidate_tree_views(docs, changed_at=observed_at)

    assert serve.discover_plans(docs, PROJECT, None) == rebuilt
    assert len(scans) == 2, (
        "a discovery newer than the reporting watcher's own observation was "
        "discarded, so every open stream pays for the scan again"
    )

    # The index rows are dropped on every report, stale discovery or not.
    assert metadata_index.index_rows(docs, PROJECT)
    assert len(builds) == 2, (
        "the reported change left the index rows in place: a page painting "
        "from /_index would read a row list that predates it"
    )


def test_the_fallback_settles_the_derived_promise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer awaiting derived readiness is never left with undefined."""

    body = f"""
{_SUMMARIZE}
window.STATE_READY.then(async state => {{
  clearTimeout(watchdog);
  const derived = await window.STATE_DERIVED_READY;
  console.log(JSON.stringify({{
    resolved: true,
    timeout: false,
    is_promise: typeof window.STATE_DERIVED_READY?.then === "function",
    is_assembled_state: derived === state,
    is_published_state: derived === window.STATE,
    keys: summary(derived).keys,
    ready_set: derived.ready_set,
  }}));
}});
"""
    observed = _run_node(_INDEX_IS_ABSENT + _DISCOVERY_ANSWERS, body)

    assert observed["resolved"] is True, observed
    assert observed["is_promise"] is True, (
        "the fallback path left window.STATE_DERIVED_READY undefined"
    )
    assert observed["is_assembled_state"] is True
    assert observed["is_published_state"] is True
    assert observed["keys"] == ["alpha", "beta", "evidence:gate"]
    assert observed["ready_set"] == DISCOVERY["ready_set"]


def test_a_discovery_only_row_is_held_as_an_arrival() -> None:
    """A row the index did not carry is not inserted into the open list."""

    body = f"""
{_SUMMARIZE}
window.STATE_READY.then(async state => {{
  clearTimeout(watchdog);
  const painted = state.inventory.slice();
  await window.STATE_DERIVED_READY;
  const arrival = window.STATE.arrival;
  console.log(JSON.stringify({{
    resolved: true,
    timeout: false,
    after: summary(window.STATE),
    same_rows: window.STATE.inventory.every((row, index) => row === painted[index]),
    row_count: window.STATE.inventory.length,
    pending: arrival.pending.map(row => row.nav_key),
    pending_title: arrival.pending.map(row => row.title),
    receipt: arrival.receipt,
    plans_keys: Object.keys(window.STATE.plans),
  }}));
}});
"""
    observed = _run_node(_INDEX_ANSWERS + _DISCOVERY_ANSWERS_WITH_ARRIVAL, body)

    assert observed["resolved"] is True, observed
    assert observed["after"]["keys"] == INDEX_ORDER, (
        "the discovery-only row was inserted into the list on screen: "
        f"{observed['after']['keys']}"
    )
    assert observed["row_count"] == len(INDEX_ROWS)
    assert observed["same_rows"] is True
    assert observed["pending"] == ["evidence:late"]
    assert observed["pending_title"] == ["Late"]
    assert observed["receipt"] == "1 new"
    assert observed["plans_keys"] == INDEX_ORDER
    # The derived fields still landed on the rows the index did carry.
    assert observed["after"]["effective_status"][:2] == ["blocked", "ready"]


# ─── A revalidation keeps the derived blocks until better ones replace them ──


_KEYS_BODY = """
window.STATE_READY.then(async state => {
  clearTimeout(watchdog);
  // The first paint's merge has landed: every derived block is on screen.
  await window.STATE_DERIVED_READY;
  const keys = window.DERIVED_STATE_KEYS.slice();
  const held = {};
  for (const key of keys) held[key] = window.STATE[key];
  // A revalidation, as a change event asks for: the index answers at once and
  // discovery follows after a delay.
  await window.revalidateProjectState();
  const afterIndex = {};
  for (const key of keys) afterIndex[key] = window.STATE[key];
  await window.STATE_DERIVED_READY;
  const afterMerge = {};
  for (const key of keys) afterMerge[key] = window.STATE[key];
  console.log(JSON.stringify({
    resolved: true,
    timeout: false,
    keys,
    held,
    after_index: afterIndex,
    after_merge: afterMerge,
  }));
});
"""


def test_a_revalidation_keeps_the_derived_blocks_until_the_merge_replaces_them() -> None:
    """The index carries no derived state, so a revalidation must not empty it.

    The page on screen already holds a schedule, sprints, a ready set and the
    rest. A revalidation reads the index, which carries rows alone, and only
    then a discovery response that carries the blocks. Between the two the kept
    set stays exactly as it was: what the reader is looking at is replaced by
    better blocks, never emptied while the better ones are on their way.
    """

    observed = _run_node(
        _DISCOVERY_DELAYED_THEN_REPLACED,
        _KEYS_BODY,
    )

    assert observed["resolved"] is True, observed
    assert observed["keys"] == DERIVED_STATE_KEYS
    for key in DERIVED_STATE_KEYS:
        assert observed["after_index"][key] == observed["held"][key], (
            f"{key} was emptied by the revalidation's index instead of being "
            f"kept until the merge: {observed['held'][key]!r} -> "
            f"{observed['after_index'][key]!r}"
        )
    # The merge is what replaced them, and the replacement is a different
    # schedule from the one kept, so the assertion above cannot be satisfied by
    # a merge that changed nothing.
    assert observed["held"]["schedule"] == SCHEDULE_FIRST
    assert observed["after_merge"]["schedule"] == SCHEDULE_REPLACEMENT


_STALE_BODY = """
window.STATE_READY.then(async state => {
  clearTimeout(watchdog);
  await window.STATE_DERIVED_READY;
  const held = window.STATE.schedule;
  await window.revalidateProjectState();
  await window.STATE_DERIVED_READY;
  const after = window.STATE.schedule;
  console.log(JSON.stringify({
    resolved: true,
    timeout: false,
    held_high: held && held.high,
    held_bars: held && held.bars,
    after_high: after && after.high,
    after_bars: after && after.bars,
    after_stale: after && after.stale,
    after_stale_error: after && after.stale_error,
    after_reference: after && after.reference,
  }));
});
"""


def test_a_null_schedule_with_an_error_keeps_the_one_on_screen_stale() -> None:
    """A discovery that could not build a schedule says so; it does not blank it.

    The read serves ``schedule`` null with ``schedule_error`` beside it. The
    blocks already on screen are the last ones actually served, so the schedule
    is kept and marked stale with that error rather than replaced by a null the
    surface would draw as "no schedule payload served".
    """

    observed = _run_node(_DISCOVERY_DELAYED_THEN_WITHHELD, _STALE_BODY)

    assert observed["resolved"] is True, observed
    assert observed["held_high"] == SCHEDULE_FIRST["high"]
    # The schedule itself is kept, bars and all, rather than replaced by null.
    assert observed["after_high"] == SCHEDULE_FIRST["high"]
    assert observed["after_bars"] == SCHEDULE_FIRST["bars"]
    assert observed["after_reference"] == SCHEDULE_FIRST["reference"]
    # And it is marked stale with the error the discovery served.
    assert observed["after_stale"] is True
    assert observed["after_stale_error"] == DISCOVERY_WITHOUT_SCHEDULE["schedule_error"]


_DERIVED_EVENT_BODY = """
window.STATE_READY.then(async state => {
  clearTimeout(watchdog);
  await window.STATE_DERIVED_READY;
  console.log(JSON.stringify({
    resolved: true,
    timeout: false,
    derived_events: dispatched.filter(name => name === "reckon:derived").length,
    dispatched: dispatched.slice(),
    requested: requested.slice(),
  }));
});
"""


def test_the_merge_announces_itself_with_a_derived_event() -> None:
    """The shell repaints on the merge, with /_checks held unanswered.

    The merge used to reach the surface only through the checks path: the shell
    asked /_checks once derived state settled and answered that with a render.
    The merge now dispatches ``reckon:derived`` when it lands, so the flow
    returns without that round trip — which is why /_checks is left unanswered
    here, and why the case asserts the checks route was never asked at all.
    """

    observed = _run_node(
        _INDEX_ANSWERS + _DISCOVERY_ANSWERS + _CHECKS_NEVER,
        _DERIVED_EVENT_BODY,
    )

    assert observed["resolved"] is True, observed
    assert observed["derived_events"] == 1, (
        "the merge did not announce itself exactly once: "
        f"{observed['dispatched']}"
    )
    # Nothing but the index and the discovery answered, so the event cannot be
    # a side effect of the checks path.
    assert observed["requested"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT]


#: The served page's own project. The fixtures answer its index and discovery
#: routes, so the rest of the page — the shell, the crew view, the loader the
#: browser actually executes — runs unmodified.
SERVED_PROJECT = "reckon"

#: A real served page whose index and discovery answers are the fixtures above.
#: The discovery is delayed so the revalidation window is long enough to sample,
#: and ``window.__withhold`` makes a later discovery serve the null schedule
#: with its failure, which is the arm that must leave the served bars on screen.
_SERVED_PRELOAD = """(() => {
  const nativeFetch = window.fetch.bind(window);
  window.__requests = [];
  const rows = %s;
  const discovery = %s;
  const replacement = %s;
  const held = () => new Promise(() => {});
  const response = payload => new Response(JSON.stringify(payload), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
  // What the page was actually answered with, per route. A route that was
  // attempted and held is recorded in __requests alone: this is the difference
  // between a repaint path that was closed and one that was merely slow.
  window.__answered = { checks: 0, crew: 0 };
  window.fetch = (resource, options) => {
    const url = String(resource);
    window.__requests.push(url);
    if (url.endsWith("/_index/%s")) return Promise.resolve(response(rows));
    // Held unanswered: the checks round trip and the crew poll are the two
    // routes a repaint could otherwise arrive on, and this case is about the
    // merge's own event. Held, the only render after the merge is the one
    // shell.jsx draws for reckon:derived.
    if (url.includes("/_checks/")) {
      if (!window.__answerChecks) return held();
      window.__answered.checks += 1;
      return Promise.resolve(response({ documents: [], checked: 0, pending: 0 }));
    }
    if (url.endsWith("/crew")) {
      if (!window.__answerCrew) return held();
      window.__answered.crew += 1;
      return Promise.resolve(response({ runs: [] }));
    }
    if (url.endsWith("/_discover/%s")) {
      const payload = window.__replacement ? replacement : discovery;
      return new Promise(resolve => setTimeout(() => {
        resolve(response(window.__withhold
          ? { ...payload, schedule: null,
              schedule_error: "RuntimeError: crew rows unavailable" }
          : payload));
      }, window.__discoverDelay || 400));
    }
    return nativeFetch(resource, options);
  };
})()""" % (
    json.dumps(INDEX_ROWS),
    json.dumps(DISCOVERY),
    json.dumps(DISCOVERY_REPLACEMENT),
    SERVED_PROJECT,
    SERVED_PROJECT,
)

#: Sample the drawn flow every few milliseconds across a revalidation. The
#: checks event is dispatched once the index has landed because that is the
#: event the shell answers with a render: without it a fast discovery could
#: merge before React ever repainted, and a missing schedule would never reach
#: the screen where this case is looking for it.
_SERVED_REVALIDATION_PROBE = """(async () => {
  const waitFor = async predicate => {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      if (predicate()) return true;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    return false;
  };
  const flow = () => document.querySelector('.r-derived-flow');
  const barCount = () => document.querySelectorAll('.r-derived-flow-bar').length;
  const drew = await waitFor(() => Boolean(flow() && barCount() > 0));
  const before = {
    drew,
    bars: barCount(),
    header: flow() ? flow().textContent.slice(0, 60) : null,
    unavailable: Boolean(flow() && flow().textContent.includes('no schedule payload served')),
  };
  // The revalidation's discovery answers a schedule with one more bar, so the
  // drawn flow can only come to show it by rendering again after the merge:
  // nothing else in this page repaints (the checks route and the crew poll are
  // both held), so the counts below measure the merge's own event.
  window.__withhold = false;
  window.__replacement = true;
  const revalidated = window.revalidateProjectState();
  const samples = [];
  const timer = setInterval(() => {
    const drawn = flow();
    samples.push({
      flow: Boolean(drawn),
      bars: barCount(),
      unavailable: drawn
        ? drawn.textContent.includes('no schedule payload served')
        : null,
      schedule: Boolean(window.STATE && window.STATE.schedule),
    });
  }, 4);
  await revalidated;
  await window.STATE_DERIVED_READY;
  await new Promise(resolve => setTimeout(resolve, 200));
  const kept = {
    samples: samples.length,
    missing: samples.filter(sample => !sample.flow).length,
    unavailable: samples.filter(sample => sample.unavailable).length,
    withoutSchedule: samples.filter(sample => !sample.schedule).length,
    bars: barCount(),
    header: flow() ? flow().textContent.slice(0, 60) : null,
  };
  clearInterval(timer);
  window.__withhold = true;
  await window.revalidateProjectState();
  await window.STATE_DERIVED_READY;
  await new Promise(resolve => setTimeout(resolve, 200));
  const stale = document.querySelector('.r-derived-flow-stale');
  return {
    before,
    kept,
    stale: {
      drawn: Boolean(stale),
      text: stale ? stale.textContent : null,
      bars: barCount(),
      unavailable: Boolean(flow() && flow().textContent.includes('no schedule payload served')),
    },
    held: {
      checks: window.__answered.checks,
      crew: window.__answered.crew,
      checks_attempted: window.__requests.filter(url => url.includes('/_checks/')).length,
      crew_attempted: window.__requests.filter(url => url.endsWith('/crew')).length,
    },
  };
})()"""


def test_a_served_revalidation_keeps_the_derived_flow_drawn(tmp_path: Path) -> None:
    """The bars stay on a served page across a revalidation, and age when stale.

    The loader cases above prove the merge announces itself; this one proves the
    page repaints on that announcement, which is the half a reader sees. The
    checks route and the crew poll are held unanswered, so the only route to a
    render after the merge is the listener ``shell.jsx`` registers for
    ``reckon:derived``: a page that answered ``/_checks`` could repaint on the
    checks path and leave that listener unpinned. The revalidation's discovery
    answers one bar more than the first load, so the count asserted below is a
    fact only a render made after the merge can bring to the screen.

    The probe prints its per-sample observations, so a run of this case leaves
    them in the run's log rather than only in the assertion that reads them.
    """

    with served_spa(
        tmp_path, installed_browser_or_skip(), project=SERVED_PROJECT, route="#crew"
    ) as spa:
        served = spa.run_probe(
            _SERVED_REVALIDATION_PROBE,
            preload_expression=_SERVED_PRELOAD,
            ready_expression="document.readyState === 'complete'",
        )

    print(json.dumps(served, sort_keys=True))

    assert served["before"]["drew"] is True, served
    assert served["before"]["bars"] == len(SCHEDULE_FIRST["bars"]), served

    kept = served["kept"]
    assert kept["samples"] > 1, (
        f"the revalidation was sampled {kept['samples']} samples, so it cannot "
        "show what the flow did while it was in flight"
    )
    assert kept["bars"] == len(SCHEDULE_TWO_BARS["bars"]), (
        f"the flow draws {kept['bars']} bars after the merge, where the "
        "discovery that landed served "
        f"{len(SCHEDULE_TWO_BARS['bars'])}: the new schedule reached the state "
        "but no render followed it, so the shell did not answer reckon:derived"
    )
    assert f"{len(SCHEDULE_TWO_BARS['bars'])} items" in kept["header"], kept
    assert kept["missing"] == 0, (
        f"the derived flow was absent in {kept['missing']} of {kept['samples']} "
        "samples taken across the revalidation"
    )
    assert kept["unavailable"] == 0, (
        f"the flow showed no schedule payload served in {kept['unavailable']} of "
        f"{kept['samples']} samples taken across the revalidation"
    )
    assert kept["withoutSchedule"] == 0, kept

    # The two routes a repaint could otherwise arrive on were both attempted and
    # never answered, which is what makes the assertion above about the merge.
    assert served["held"]["checks_attempted"] >= 1, served
    assert served["held"]["crew_attempted"] >= 1, served
    # Held rather than merely attempted, which is the difference that makes the
    # assertion above about the merge's event: an answered checks route renders
    # the page on its own path, and a page that answered it could repaint there
    # instead of on `reckon:derived`.
    assert served["held"]["checks"] == 0, (
        "the checks route was answered, so a render could have arrived on it "
        "rather than on the merge's own event"
    )
    assert served["held"]["crew"] == 0, (
        "the crew poll was answered, so a render could have arrived on it "
        "rather than on the merge's own event"
    )

    # A discovery that builds no schedule leaves the served bars up and marks
    # them stale with the failure, rather than drawing an empty flow.
    assert served["stale"]["drawn"] is True, served
    assert "RuntimeError: crew rows unavailable" in served["stale"]["text"], served
    assert "last served" in served["stale"]["text"], served
    assert served["stale"]["bars"] == len(SCHEDULE_TWO_BARS["bars"]), served
    assert served["stale"]["unavailable"] is False, served


# ─── The flow refreshes from its own route ───────────────────────────────


#: Two crew polls, then a change event. A fleet event says nothing about this
#: project's documents, so it must dispatch its key and ask for no refetch; a
#: change event is still the cue the project's own revalidation waits on. The
#: settle window is 500 ms, so each wait below outlasts it.
_STREAM_BODY = """
window.STATE_READY.then(async () => {
  clearTimeout(watchdog);
  await window.STATE_DERIVED_READY;
  let refreshes = 0;
  const stream = window.watchProjectStateChanges(() => { refreshes += 1; });
  stream.emit("fleet", { fleet: { key: "rollup" } });
  await new Promise(resolve => setTimeout(resolve, 900));
  const afterFleet = refreshes;
  const fleetEvents = dispatched.filter(name => name === "reckon-fleet").length;
  stream.emit("change", { rows: { changed: [], added: [], removed: [] } });
  await new Promise(resolve => setTimeout(resolve, 900));
  console.log(JSON.stringify({
    resolved: true,
    timeout: false,
    after_fleet: afterFleet,
    after_change: refreshes,
    fleet_events: fleetEvents,
    requested: requested.slice(),
  }));
});
"""


def test_a_fleet_event_dispatches_its_key_and_refreshes_nothing() -> None:
    """A fleet event is not a fact about this project, so it revalidates nothing.

    The rollup over every mount has no owning project, and with many mounts and
    live crews those events arrive constantly. The key still reaches a view
    computing it; the derived refetch they used to ask for does not, and a
    change event still does — one revalidation, once per settle window.
    """

    observed = _run_node(
        _INDEX_ANSWERS + _DISCOVERY_ANSWERS, _STREAM_BODY, timeout_ms=5000
    )

    assert observed["resolved"] is True, observed
    assert observed["fleet_events"] == 1, (
        "the fleet key did not reach the window exactly once: "
        f"{observed['dispatched']} event(s)"
    )
    assert observed["after_fleet"] == 0, (
        "a fleet event still refreshed the project's derived state: "
        f"{observed['after_fleet']} refetch(es) within the settle window"
    )
    # The positive control: the same stream, with a change event, still
    # refreshes — so the assertion above is about the fleet event and not about
    # a listener that stopped answering altogether.
    assert observed["after_change"] == 1, (
        f"a change event refreshed {observed['after_change']} times, not once"
    )
    # Nothing but the index and the discovery was asked for, so the refresh the
    # change event made is the only request this case's stream could have made.
    assert observed["requested"] == [INDEX_ENDPOINT, DISCOVERY_ENDPOINT], observed


#: The due decision driven with a supplied clock, then the fetch driven by it.
#: The first poll has nothing built for the view; the second shows the run in a
#: different phase; the third and fourth leave it unchanged, one inside the
#: minute and one past it.
_SCHEDULE_DUE_BODY = """
window.STATE_READY.then(async () => {
  clearTimeout(watchdog);
  await window.STATE_DERIVED_READY;
  const held = window.STATE.schedule;
  const T0 = 1000000000000;
  const working = [{ project: "sample", run_id: 41, phase: "working" }];
  const verifying = [{ project: "sample", run_id: 41, phase: "completed_unpromoted" }];
  const scheduleRequests = () =>
    requested.filter(url => url === "/_schedule/sample").length;
  const dueFirst = window.scheduleFetchDue(working, "sample", T0);
  await window.refreshSchedule("sample", working, T0);
  const afterFirst = scheduleRequests();
  const dueChanged = window.scheduleFetchDue(verifying, "sample", T0 + 3000);
  await window.refreshSchedule("sample", verifying, T0 + 3000);
  const afterChanged = scheduleRequests();
  const dueWithin = window.scheduleFetchDue(verifying, "sample", T0 + 33000);
  const afterWithin = scheduleRequests();
  const dueAfter = window.scheduleFetchDue(verifying, "sample", T0 + 63000);
  await window.refreshSchedule("sample", verifying, T0 + 63000);
  const afterMinute = scheduleRequests();
  const served = window.STATE.schedule;
  console.log(JSON.stringify({
    resolved: true,
    timeout: false,
    due_first: dueFirst,
    after_first: afterFirst,
    due_changed: dueChanged,
    after_changed: afterChanged,
    due_within: dueWithin,
    after_within: afterWithin,
    due_after: dueAfter,
    after_minute: afterMinute,
    replaced: served === DATA.schedule_route,
    unfetched_kept: held === DATA.discovery.schedule,
    held_reference: held && held.reference,
    served_reference: served && served.reference,
    served_state: served && served.bars[0].state,
    requested: requested.slice(),
  }));
});
"""


def test_the_schedule_fetch_is_due_on_a_phase_change_and_once_a_minute() -> None:
    """The poll decides whether the schedule is due, and the fetch replaces it.

    A poll that shows the same runs in the same phases cannot have moved a bar,
    so it costs no request; a poll that shows a run in a different phase fetches
    at once; and an unchanged view re-anchors once a minute so its now line
    cannot drift from the clock. What the route answers is what lands on
    ``window.STATE``, and the state the view held before is the control: it is
    unchanged only because nothing fetched, so the replacement assertion cannot
    be satisfied by reading a state that was never replaced.
    """

    observed = _run_node(_SCHEDULE_ANSWERS, _SCHEDULE_DUE_BODY)

    assert observed["resolved"] is True, observed
    assert observed["due_first"] is True, observed
    assert observed["after_first"] == 1, observed

    # A changed phase: due, and exactly one request.
    assert observed["due_changed"] is True, observed
    assert observed["after_changed"] == observed["after_first"] + 1, observed

    # Unchanged, inside the minute: not due, and no request at all.
    assert observed["due_within"] is False, (
        "an unchanged poll inside the minute asked for the schedule again"
    )
    assert observed["after_within"] == observed["after_changed"], observed

    # Unchanged, past the minute: due, and exactly one request, the re-anchor.
    assert observed["due_after"] is True, (
        "an unchanged poll past the minute did not re-anchor the schedule"
    )
    assert observed["after_minute"] == observed["after_changed"] + 1, observed

    # The fetched schedule is the one on the state, and it is the route's own
    # answer rather than the one discovery served.
    assert observed["replaced"] is True, observed
    assert observed["served_reference"] == SCHEDULE_FROM_ROUTE["reference"], observed
    assert observed["served_state"] == "in-flight", observed
    # The control: the schedule the view held before any fetch is still the
    # discovery's, so `replaced` above is a fact about the fetch.
    assert observed["unfetched_kept"] is True, observed
    assert observed["held_reference"] == SCHEDULE_FIRST["reference"], observed
    assert observed["held_reference"] != observed["served_reference"], observed


#: A served page whose crew poll answers a run list whose phase the probe moves,
#: and whose schedule route answers the schedule the flow is meant to refresh to.
#: Everything else falls through to the real server, so the shell, the crew view
#: and the loader the browser executes are all unmodified.
_SCHEDULE_BROWSER_PRELOAD = """(() => {
  const P = __PROJECT__;
  const nativeFetch = window.fetch.bind(window);
  window.__requests = [];
  window.__crewPhase = "working";
  const rows = __ROWS__;
  const discovery = __DISCOVERY__;
  const schedule = __SCHEDULE__;
  const response = payload => new Response(JSON.stringify(payload), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
  window.fetch = (resource, options) => {
    const url = String(resource);
    window.__requests.push(url);
    if (url.endsWith("/_index/" + P)) return Promise.resolve(response(rows));
    if (url.endsWith("/_discover/" + P)) return Promise.resolve(response(discovery));
    if (url.endsWith("/_schedule/" + P)) {
      return Promise.resolve(response({
        project: P,
        schedule: { ...schedule, reference: new Date().toISOString() },
      }));
    }
    if (url.includes("/_checks/")) {
      return Promise.resolve(response({ documents: [], checked: 0, pending: 0 }));
    }
    if (url.endsWith("/crew")) {
      return Promise.resolve(response({
        project: P,
        runs: [{ project: P, run_id: "r-1", plan: "alpha", phase: window.__crewPhase }],
      }));
    }
    return nativeFetch(resource, options);
  };
})()"""

#: Poll the counts across a phase change. The first poll has nothing built for
#: the view, so it fetches; the run's phase then moves, so the next poll fetches
#: once more; and a further, unchanged poll must fetch nothing.
_SCHEDULE_BROWSER_PROBE = """(async () => {
  const waitFor = async (predicate, budget) => {
    const deadline = Date.now() + budget;
    while (Date.now() < deadline) {
      if (predicate()) return true;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return false;
  };
  const scheduleCount = () =>
    window.__requests.filter(url => url.includes("/_schedule/")).length;
  const crewCount = () => window.__requests.filter(url => url.endsWith("/crew")).length;
  await waitFor(() => crewCount() >= 1 && scheduleCount() >= 1, 20000);
  const first = scheduleCount();
  const crewsBefore = crewCount();
  window.__crewPhase = "completed_unpromoted";
  await waitFor(() => crewCount() > crewsBefore && scheduleCount() > first, 20000);
  const afterChange = scheduleCount();
  const crewsAtChange = crewCount();
  await waitFor(() => crewCount() > crewsAtChange, 20000);
  return {
    first,
    after_change: afterChange,
    settled: scheduleCount(),
    crew_requests: crewCount(),
    replaced: Boolean(window.STATE && window.STATE.schedule),
  };
})()"""

#: The same page on a route that draws no flow. The poll the shell owns still
#: runs, so a schedule request there would be the poll's doing and not the
#: route's.
_OTHER_ROUTE_PROBE = """(async () => {
  const waitFor = async (predicate, budget) => {
    const deadline = Date.now() + budget;
    while (Date.now() < deadline) {
      if (predicate()) return true;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return false;
  };
  const scheduleCount = () =>
    window.__requests.filter(url => url.includes("/_schedule/")).length;
  const crewCount = () => window.__requests.filter(url => url.endsWith("/crew")).length;
  await waitFor(() => crewCount() >= 1, 20000);
  window.__crewPhase = "completed_unpromoted";
  await waitFor(() => crewCount() >= 2, 20000);
  return {
    crew_requests: crewCount(),
    schedule_requests: scheduleCount(),
    hash: window.location.hash,
  };
})()"""


def _schedule_preload() -> str:
    return (
        _SCHEDULE_BROWSER_PRELOAD.replace("__PROJECT__", json.dumps(SERVED_PROJECT))
        .replace("__ROWS__", json.dumps(INDEX_ROWS))
        .replace("__DISCOVERY__", json.dumps(DISCOVERY))
        .replace("__SCHEDULE__", json.dumps(SCHEDULE_FROM_ROUTE))
    )


def test_the_crew_view_refreshes_the_schedule_from_its_own_route(
    tmp_path: Path,
) -> None:
    """The shell's own /_schedule request follows the runs the poll serves.

    The crew view draws the flow, so it is the view that asks for the schedule:
    one request when the poll shows a run whose phase has moved since the
    schedule was built, none for a further poll that shows nothing new, and
    none at all on a route that draws no flow however often it polls.
    """

    with served_spa(
        tmp_path,
        installed_browser_or_skip(),
        project=SERVED_PROJECT,
        route="#crew",
    ) as spa:
        served = spa.run_probe(
            _SCHEDULE_BROWSER_PROBE,
            preload_expression=_schedule_preload(),
            ready_expression="document.readyState === 'complete'",
        )

    print(json.dumps(served, sort_keys=True))

    assert served["first"] >= 1, served
    assert served["after_change"] == served["first"] + 1, (
        "a poll showing a run in a moved phase asked for the schedule "
        f"{served['after_change'] - served['first']} times, not once: {served}"
    )
    assert served["settled"] == served["after_change"], (
        f"a further poll that showed nothing new asked for the schedule again: {served}"
    )
    assert served["crew_requests"] >= 2, served


def test_a_route_that_draws_no_flow_asks_for_no_schedule(tmp_path: Path) -> None:
    """The poll runs on every route; only the crew view's asks for the schedule.

    The control for the case above: the same page, the same poll, on a route
    that draws no derived flow, makes no request at all.
    """

    with served_spa(
        tmp_path, installed_browser_or_skip(), project=SERVED_PROJECT, route="#cockpit"
    ) as spa:
        served = spa.run_probe(
            _OTHER_ROUTE_PROBE,
            preload_expression=_schedule_preload(),
            ready_expression="document.readyState === 'complete'",
        )

    print(json.dumps(served, sort_keys=True))

    # The positive control: the poll the shell owns ran here too, so an
    # unanswered schedule route is a decision and not a page that never polled.
    assert served["crew_requests"] >= 2, served
    assert served["schedule_requests"] == 0, (
        "a route that draws no flow asked for the schedule: "
        f"{served['schedule_requests']} request(s)"
    )


#: The route answering a null schedule beside its failure, which is what the
#: server does when the crew rows cannot be read: the stored inventory is
#: already in hand, so the read keeps its 200 and loses only the schedule.
_SCHEDULE_WITHHELD = """
  if (url === "/_index/sample") {
    requested.push(url);
    return ok(DATA.index);
  }
  if (url === "/_discover/sample") {
    requested.push(url);
    return ok(DATA.discovery);
  }
  if (url === "/_schedule/sample") {
    requested.push(url);
    return ok({ project: "sample", schedule: null, schedule_error: "RuntimeError: crew rows unavailable" });
  }
"""

#: The crew poll answers every mounted project's runs, so the due test is driven
#: here with a foreign run that moves phase and with this project's own run.
_SCHEDULE_PROJECT_SCOPE_BODY = """
window.STATE_READY.then(async () => {
  clearTimeout(watchdog);
  await window.STATE_DERIVED_READY;
  const T0 = 1000000000000;
  const mine = [{ project: "sample", run_id: 41, phase: "working" }];
  const foreignWorking = [
    { project: "sample", run_id: 41, phase: "working" },
    { project: "other", run_id: 77, phase: "working" },
  ];
  const foreignMoved = [
    { project: "sample", run_id: 41, phase: "working" },
    { project: "other", run_id: 77, phase: "completed_unpromoted" },
  ];
  const mineMoved = [
    { project: "sample", run_id: 41, phase: "completed_unpromoted" },
    { project: "other", run_id: 77, phase: "completed_unpromoted" },
  ];
  const scheduleRequests = () =>
    requested.filter(url => url === "/_schedule/sample").length;
  await window.refreshSchedule("sample", foreignWorking, T0);
  const afterFirst = scheduleRequests();
  const dueForeign = window.scheduleFetchDue(foreignMoved, "sample", T0 + 3000);
  const afterForeign = scheduleRequests();
  const dueMine = window.scheduleFetchDue(mineMoved, "sample", T0 + 6000);
  await window.refreshSchedule("sample", mineMoved, T0 + 6000);
  const afterMine = scheduleRequests();
  const unscoped = window.scheduleRunSignature(foreignMoved);
  const scoped = window.scheduleRunSignature(foreignMoved, "sample");
  console.log(JSON.stringify({
    resolved: true,
    timeout: false,
    after_first: afterFirst,
    due_foreign: dueForeign,
    after_foreign: afterForeign,
    due_mine: dueMine,
    after_mine: afterMine,
    unscoped_differs: unscoped !== window.scheduleRunSignature(foreignWorking),
    scoped_same: scoped === window.scheduleRunSignature(foreignWorking, "sample"),
    requested: requested.slice(),
  }));
});
"""


def test_only_this_projects_runs_make_the_schedule_due() -> None:
    """The due test reads this project's runs, not every mount's.

    The crew poll answers every mounted project's runs, and the schedule it
    fetches is one project's. A run of another project that moves phase cannot
    have moved a bar of this project's flow, so it must cost no request; only a
    move among this project's own runs makes the schedule due.
    """

    observed = _run_node(_SCHEDULE_ANSWERS, _SCHEDULE_PROJECT_SCOPE_BODY)

    assert observed["resolved"] is True, observed
    assert observed["due_foreign"] is False, (
        "another project's run moving phase made this project's schedule due"
    )
    assert observed["after_foreign"] == observed["after_first"], observed

    # The positive control: the same poll shape, with this project's own run
    # moved, is due and fetches once — so the assertion above is about which
    # project's runs the due test reads and not about a poll that stopped
    # asking altogether.
    assert observed["due_mine"] is True, observed
    assert observed["after_mine"] == observed["after_foreign"] + 1, observed

    # What the filter does, read directly: the unscoped signature moves when a
    # foreign run moves and the scoped one does not, so the case above is not
    # passing because the two run lists happen to agree.
    assert observed["unscoped_differs"] is True, observed
    assert observed["scoped_same"] is True, observed


#: The route's failure branch: a 200 with a null schedule and its error. The
#: schedule on screen is the last that was actually served, so it is kept and
#: marked stale rather than emptied.
_SCHEDULE_WITHHELD_BODY = """
window.STATE_READY.then(async () => {
  clearTimeout(watchdog);
  await window.STATE_DERIVED_READY;
  const held = window.STATE.schedule;
  const heldBars = held.bars.length;
  held.stale = false;
  held.stale_error = null;
  const T0 = 1000000000000;
  const runs = [{ project: "sample", run_id: 41, phase: "working" }];
  const returned = await window.refreshSchedule("sample", runs, T0);
  const kept = window.STATE.schedule;
  console.log(JSON.stringify({
    resolved: true,
    timeout: false,
    same_object: kept === held,
    returned_same: returned === held,
    bars: kept.bars.length,
    held_bars: heldBars,
    reference: kept.reference,
    held_reference: DATA.discovery.schedule.reference,
    stale: kept.stale === true,
    stale_error: kept.stale_error,
    schedule_requests: requested.filter(url => url === "/_schedule/sample").length,
  }));
});
"""


def test_a_withheld_schedule_keeps_the_one_on_screen_and_marks_it_stale() -> None:
    """A route that answers no schedule costs the schedule and not the picture.

    The route answers 200 with a null schedule and the failure beside it when
    the live crew rows cannot be read. The schedule on screen is the last that
    was actually served, so the loader keeps it and marks it stale with that
    error — an emptied flow is what the keeping exists to prevent.
    """

    observed = _run_node(_SCHEDULE_WITHHELD, _SCHEDULE_WITHHELD_BODY)

    assert observed["resolved"] is True, observed
    # The request was made, so the answer under test is the route's own.
    assert observed["schedule_requests"] == 1, observed
    assert observed["same_object"] is True, (
        f"the schedule on screen was replaced by the withheld answer: {observed}"
    )
    assert observed["returned_same"] is True, observed
    assert observed["bars"] == observed["held_bars"], observed
    assert observed["stale"] is True, (
        "the kept schedule was not marked stale, so it reads as current"
    )
    assert observed["stale_error"] == "RuntimeError: crew rows unavailable", observed
    # It is the schedule discovery served, unchanged but for the marking: a
    # different reference would mean the picture moved for a read that failed.
    assert observed["reference"] == observed["held_reference"], observed
