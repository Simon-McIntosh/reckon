"""A derived route answers from the last result, never behind a recompute.

Discovery, the checks summary and the fleet rollup each hold the interpreter
deriving them. These tests pin the stored-result contract a request reads
from: a recompute runs in a child process while the route keeps answering the
prior result with its age, one rollup child serves a change to several
projects, and a fleet-scoped result reaches every open change stream when it
changes.

``RECKON_SLOW_ROUTES_INLINE=1`` applies the declared mutation — each route
computes inline under the same delay — under which the latency bound fails.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from reckon import metadata_index, route_results, serve

PROJECT = "sample"
OTHER = "other"
#: The recompute a route must not wait behind, and the bound its own answer
#: must stay inside while that recompute runs.
DELAY = 0.35
BOUND = 0.05
_INLINE_MUTATION = "RECKON_SLOW_ROUTES_INLINE"
_PER_PROJECT_MUTATION = "RECKON_SLOW_ROUTES_PER_PROJECT"
_PER_PROJECT_EVENTS_MUTATION = "RECKON_SLOW_ROUTES_PER_PROJECT_EVENTS"


def _plan_page(project: str, slug: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{project}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="active">
<title>{slug}</title></head><body><main class="plan-doc">
<section id="s1"><h2 id="h1">§1 — A section</h2><p>Prose.</p></section>
</main></body></html>
"""


def _project(root: Path, name: str, slugs: tuple[str, ...]) -> Path:
    docs = root / name / "docs"
    (docs / "plans").mkdir(parents=True)
    for slug in slugs:
        (docs / "plans" / f"{slug}.html").write_text(
            _plan_page(name, slug), encoding="utf-8"
        )
    return docs


class _Harness:
    def __init__(self, port: int, mounts: dict[str, Path], mounts_file: Path) -> None:
        self.port = port
        self.mounts = mounts
        self.mounts_file = mounts_file

    def get(self, path: str) -> tuple[int, object, dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = json.loads(response.read())
            headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, body, headers
        finally:
            connection.close()


def _apply_inline_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compute each slow route inline under the same delay.

    The declared mutation: with no stored result readable, a route derives its
    payload on the request path, so its own answer takes the recompute's delay
    and the bound fails.
    """

    def slow_compute(key):
        time.sleep(DELAY)
        return {"marker": "inline", "pending": 0}

    monkeypatch.setattr(route_results, "_refresh_result", lambda key: None)
    monkeypatch.setattr(route_results, "_route_compute", slow_compute)
    monkeypatch.setattr(route_results, "_compute_refresh", slow_compute)


def _apply_per_project_events_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish only per-project events, so no fleet event reaches a stream."""

    monkeypatch.setattr(route_results, "_is_fleet_key", lambda key: False)


def _apply_per_project_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key the rollup per project, so a change to two starts two children."""

    def per_project(self, tree: Path) -> None:
        project = self._projects.get(Path(tree).resolve(), "")
        if project:
            route_results._start_refresh(("discover", project))
            route_results._start_refresh(("rollup", project))

    monkeypatch.setattr(serve._FleetChangeWatch, "_spawn_changes", per_project)


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docs = _project(tmp_path, PROJECT, ("alpha", "beta"))
    other_docs = _project(tmp_path, OTHER, ("gamma",))
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(
        json.dumps({PROJECT: str(docs), OTHER: str(other_docs)}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    snapshot = (serve._MOUNTS_FILE, serve._STATE_ROOT, serve._SHARED_ROOT)
    serve._resolve_paths(mounts_file)
    monkeypatch.setattr(route_results, "_REFRESH_ENABLED", True)
    route_results._REFRESH_CHILDREN.clear()
    metadata_index.clear()
    serve._DISC_CACHE.clear()
    if os.environ.get(_INLINE_MUTATION):
        _apply_inline_mutation(monkeypatch)
    if os.environ.get(_PER_PROJECT_MUTATION):
        _apply_per_project_mutation(monkeypatch)
    if os.environ.get(_PER_PROJECT_EVENTS_MUTATION):
        _apply_per_project_events_mutation(monkeypatch)
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Harness(
            server.server_port, {PROJECT: docs, OTHER: other_docs}, mounts_file
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve._MOUNTS_FILE, serve._STATE_ROOT, serve._SHARED_ROOT = snapshot
        # One watch per served process: drop the one this case started.
        serve._FLEET_WATCH = None
        route_results._REFRESH_CHILDREN.clear()
        metadata_index.clear()
        serve._DISC_CACHE.clear()


# ── Each route answers from the prior result while a recompute runs ───────


def _latency_case(
    harness: _Harness, key: tuple[str, ...], path: str, monkeypatch
) -> None:
    prior = {"marker": "prior", "pending": 0}
    recomputed = {"marker": "recomputed", "pending": 0}
    route_results._store_refresh_result(key, prior)
    done = threading.Event()

    def slow_spawn(argv):
        def run():
            time.sleep(DELAY)
            route_results._store_refresh_result(key, recomputed)
            done.set()

        threading.Thread(target=run, daemon=True).start()

        class _Child:
            def poll(self):
                return None if not done.is_set() else 0

            def wait(self, timeout=None):
                done.wait(timeout)

        return _Child()

    monkeypatch.setattr(route_results, "_spawn_refresh", slow_spawn)
    route_results._REFRESH_CHILDREN.clear()
    assert route_results._start_refresh(key) is True

    started = time.monotonic()
    status, body, headers = harness.get(path)
    elapsed = time.monotonic() - started

    assert status == 200
    assert body["marker"] == "prior"
    assert elapsed < BOUND, f"{path} waited {elapsed:.3f}s behind the recompute"
    assert "x-result-age" in headers

    assert done.wait(5), "the stubbed recompute never finished"
    status, body, _ = harness.get(path)
    assert status == 200
    assert body["marker"] == "recomputed"


def test_discovery_answers_from_the_prior_result(served, monkeypatch):
    _latency_case(served, ("discover", PROJECT), f"/_discover/{PROJECT}", monkeypatch)


def test_the_checks_summary_answers_from_the_prior_result(served, monkeypatch):
    _latency_case(served, ("checks", PROJECT), f"/_checks/{PROJECT}", monkeypatch)


def test_the_fleet_rollup_answers_from_the_prior_result(served, monkeypatch):
    _latency_case(served, ("rollup",), "/_projects/index.json", monkeypatch)


# ── The change stream reads the stored discovery ─────────────────────────


def test_the_stream_reads_the_store_and_spawns_only_on_a_miss(served, monkeypatch):
    spawned: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        route_results, "_start_refresh", lambda key: spawned.append(key) or True
    )

    assert route_results._stored_discovery(PROJECT, served.mounts[PROJECT]) is None
    assert spawned == [("discover", PROJECT)]

    spawned.clear()
    route_results._store_refresh_result(
        ("discover", PROJECT), {"provenance": {"content_digest": "d"}}
    )
    assert route_results._stored_discovery(PROJECT, served.mounts[PROJECT]) == {
        "provenance": {"content_digest": "d"}
    }
    assert spawned == []


# ── One rollup child for a change to two projects ─────────────────────────


def _record_spawns(monkeypatch) -> list[str]:
    """Record the key of each child actually started, through the real
    single-flight, so one key running twice would show as one child."""

    keys: list[str] = []

    class _Child:
        def poll(self):
            return None

        def wait(self, timeout=None):
            threading.Event().wait()

    def spawn(argv):
        keys.append(argv[argv.index("--key") + 1])
        return _Child()

    monkeypatch.setattr(route_results, "_spawn_refresh", spawn)
    route_results._REFRESH_CHILDREN.clear()
    return keys


def test_two_projects_changing_at_once_start_one_rollup_child(served, monkeypatch):
    keys = _record_spawns(monkeypatch)
    watch = serve._FleetChangeWatch(served.mounts)
    watch._spawn_changes(served.mounts[PROJECT])
    watch._spawn_changes(served.mounts[OTHER])

    assert keys.count("rollup") == 1, keys
    assert f"discover:{PROJECT}" in keys
    assert f"discover:{OTHER}" in keys


# ── A fleet event reaches an open stream through the parent's waiter ──────


class _Stream:
    def __init__(self, port: int, project: str) -> None:
        self._connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
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


def test_a_fleet_event_reaches_a_stream_through_the_parent_waiter(served, monkeypatch):
    fleet = serve.start_fleet_change_watch(served.mounts)
    stream = _Stream(served.port, PROJECT)
    try:
        assert stream.next_event()[0] == "ready"
        route_results._store_refresh_result(("rollup",), {"updated": "v0"})
        assert route_results._start_refresh(("rollup",)) is True

        deadline = time.monotonic() + 20
        fleet_events: list[dict] = []
        while time.monotonic() < deadline and not fleet_events:
            event, payload = stream.next_event()
            if event == "fleet":
                fleet_events.append(payload or {})
    finally:
        stream.close()
        fleet.close()
        serve._FLEET_WATCH = None

    assert [entry.get("fleet", {}).get("key") for entry in fleet_events] == ["rollup"]
