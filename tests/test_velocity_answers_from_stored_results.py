"""Velocity requests reuse stored results while a child recomputes them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest

from reckon import route_results, serve, velocity
from tests.test_the_slow_routes_answer_from_stored_results import (
    PROJECT,
    _Stream,
)
from tests.test_the_slow_routes_answer_from_stored_results import (
    served as _served,
)


@pytest.fixture
def served(tmp_path, monkeypatch):
    yield from _served.__wrapped__(tmp_path, monkeypatch)


def test_a_cold_fleet_request_returns_pending_without_waiting(served, monkeypatch):
    def slow_view(*args, **kwargs):
        time.sleep(0.25)
        return {"ok": True, "window": {"start": "a", "end": "b"}}

    monkeypatch.setattr(velocity, "view", slow_view)
    monkeypatch.setattr(route_results, "_start_refresh", lambda key: True)

    started = time.monotonic()
    status, body, headers = served.get("/crew/velocity?project=*")
    elapsed = time.monotonic() - started

    assert status == 202
    assert elapsed < 0.05, f"cold velocity request waited {elapsed:.3f}s"
    assert headers["retry-after"]
    assert body["status"] == "computing"


def test_cold_fleet_child_stores_the_result_it_computed(served):
    for docs in served.mounts.values():
        repo = docs.parent
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "docs"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "chore: seed temporary docs\n\nProvide a commit for the velocity window.",
            ],
            check=True,
        )
    status, pending, _ = served.get("/crew/velocity?project=*&fields=plans")
    assert status == 202
    key = route_results.velocity_key("*", None, None, "plans", None, None)
    assert pending["key"] == ":".join(key)

    deadline = time.monotonic() + 30
    stored = None
    while time.monotonic() < deadline:
        stored = route_results._refresh_result(key)
        if stored is not None:
            break
        time.sleep(0.1)
    assert stored is not None, "the child did not store a velocity answer"
    assert stored[0]["ok"] is True
    status, body, headers = served.get("/crew/velocity?project=*&fields=plans")
    assert status == 200
    assert body["window"] == stored[0]["window"]
    assert "x-result-age" in headers


def test_windows_with_different_limits_keep_separate_results(served):
    since, until = "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"
    first = route_results.velocity_key(PROJECT, since, until, "plans", 2, None)
    second = route_results.velocity_key(PROJECT, since, until, "plans", 3, None)
    assert first != second
    route_results._store_refresh_result(first, {"ok": True, "marker": "two"})
    route_results._store_refresh_result(second, {"ok": True, "marker": "three"})

    base = f"/crew/velocity?project={PROJECT}&since={quote(since)}&until={quote(until)}&fields=plans"
    status, two, headers = served.get(base + "&limit=2")
    other_status, three, other_headers = served.get(base + "&limit=3")

    assert (status, other_status) == (200, 200)
    assert (two["marker"], three["marker"]) == ("two", "three")
    assert "x-result-age" in headers and "x-result-age" in other_headers


def test_stale_default_answer_is_served_while_refresh_starts(served, monkeypatch):
    key = route_results.velocity_key(PROJECT, None, None, "plans", None, None)
    old_end = velocity.iso((datetime.now(UTC) - timedelta(minutes=16)).timestamp())
    route_results._store_refresh_result(
        key,
        {
            "ok": True,
            "by_project": [{"marker": "prior"}],
            "window": {"end": old_end},
        },
    )
    spawned = []
    monkeypatch.setattr(
        route_results, "_start_refresh", lambda target: spawned.append(target) or True
    )

    status, body, headers = served.get(f"/crew/velocity?project={PROJECT}&fields=plans")

    assert status == 200
    assert body["by_project"][0]["marker"] == "prior"
    assert "x-result-age" in headers
    assert spawned == [key]


def test_default_window_uses_one_key_per_scope_and_selects_requested_blocks(served):
    key = route_results.velocity_key(PROJECT, None, None, None, None, None)
    assert key == route_results.velocity_key(PROJECT, None, None, "plans", 2, "cursor")
    route_results._store_refresh_result(
        key,
        {
            "ok": True,
            "window": {"end": velocity.iso(time.time())},
            "by_project": [],
            "by_lane": [],
            "by_day": [],
            "plans": {"marker": "stored"},
        },
    )

    status, plain, _ = served.get(f"/crew/velocity?project={PROJECT}")
    other_status, selected, _ = served.get(
        f"/crew/velocity?project={PROJECT}&fields=plans"
    )

    assert (status, other_status) == (200, 200)
    assert "plans" not in plain
    assert selected["plans"]["marker"] == "stored"


def test_default_refresh_computes_every_optional_block_once(served, monkeypatch):
    seen = []

    def view(project, **kwargs):
        seen.append((project, kwargs))
        return {
            "ok": True,
            "window": {"end": kwargs["until"]},
            "plans": {"marker": "child"},
        }

    monkeypatch.setattr(velocity, "view", view)
    key = route_results.velocity_key(PROJECT, None, None, None, None, None)
    result = route_results._compute_refresh(key)
    route_results._store_refresh_result(key, result)

    assert seen[0][0] == PROJECT
    assert set(seen[0][1]["fields"].split(",")) == velocity.OPTIONAL_BLOCKS
    assert seen[0][1]["limit"] is None
    status, body, _ = served.get(f"/crew/velocity?project={PROJECT}&fields=plans")
    assert status == 200
    assert body["plans"]["marker"] == "child"


def test_only_one_velocity_child_runs_for_a_scope(served, monkeypatch):
    children = []

    def spawn(_argv):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
        children.append(child)
        return child

    monkeypatch.setattr(route_results, "_spawn_refresh", spawn)
    first = route_results.velocity_key(PROJECT, None, None, None, None, None)
    second = route_results.velocity_key(PROJECT, None, None, "plans", None, None)
    assert route_results._start_refresh(first)
    assert route_results._start_refresh(second)
    assert len(children) == 1
    children[0].wait(timeout=5)


def test_child_completion_publishes_a_fleet_velocity_key(served, monkeypatch):
    fleet = serve.start_fleet_change_watch(served.mounts)
    stream = _Stream(served.port, PROJECT)
    key = route_results.velocity_key("*", None, None, "plans", None, None)
    path = route_results._refresh_store_path(key)
    route_results._store_refresh_result(key, {"ok": True, "marker": "prior"})

    def spawn(_argv):
        payload = json.dumps(
            {
                "schema": "reckon-route-result",
                "key": list(key),
                "version": "child-finished",
                "result": {"ok": True, "marker": "child"},
            }
        )
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib,sys,time;time.sleep(.2);pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                str(path),
                payload,
            ]
        )

    monkeypatch.setattr(route_results, "_spawn_refresh", spawn)
    if os.environ.get("RECKON_VELOCITY_PROJECT_EVENTS_ONLY"):
        monkeypatch.setattr(route_results, "_is_fleet_key", lambda _key: False)
    try:
        assert stream.next_event()[0] == "ready"
        assert route_results._start_refresh(key)
        publication_deadline = time.monotonic() + 5
        while time.monotonic() < publication_deadline and not fleet.fleet_versions():
            time.sleep(0.05)
        assert ":".join(key) in fleet.fleet_versions()
        deadline = time.monotonic() + 10
        fleet_events = []
        while time.monotonic() < deadline and not fleet_events:
            event, payload = stream.next_event()
            if event == "fleet":
                fleet_events.append(payload)
    finally:
        stream.close()
        fleet.close()
        serve._FLEET_WATCH = None

    assert [entry["fleet"]["key"] for entry in fleet_events] == [":".join(key)]
    assert route_results._refresh_result(key)[0]["marker"] == "child"
