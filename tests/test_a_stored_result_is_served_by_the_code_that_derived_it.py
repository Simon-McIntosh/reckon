"""A route result belongs to the source revision that requested its refresh."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reckon import route_results, serve
from tests.test_the_slow_routes_answer_from_stored_results import (
    PROJECT,
)
from tests.test_the_slow_routes_answer_from_stored_results import (
    served as _served,
)


@pytest.fixture
def served(tmp_path, monkeypatch):
    yield from _served.__wrapped__(tmp_path, monkeypatch)


def test_discovery_from_another_revision_is_a_miss(served, monkeypatch):
    key = ("discover", PROJECT)
    spawned = []
    monkeypatch.setattr(serve.Handler, "_refuse_stale_code", lambda self: False)
    monkeypatch.setattr(
        route_results, "_start_refresh", lambda target: spawned.append(target) or True
    )
    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", SimpleNamespace(revision="first"))
    route_results._store_refresh_result(key, {"marker": "old"})

    status, body, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200
    assert body["marker"] == "old"
    assert spawned == []

    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", SimpleNamespace(revision="second"))
    status, body, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200
    assert body.get("marker") != "old"
    assert spawned == [key]

    payload_path = route_results._refresh_store_path(key)
    payload = json.loads(payload_path.read_text())
    payload.pop("source_revision")
    payload_path.write_text(json.dumps(payload))
    spawned.clear()
    status, body, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200
    assert body.get("marker") != "old"
    assert spawned == [key]


def test_refresh_child_stores_its_parents_revision(served, monkeypatch):
    key = ("discover", PROJECT)
    revision = "parent-revision"
    monkeypatch.setattr(serve, "_SOURCE_SNAPSHOT", SimpleNamespace(revision=revision))
    monkeypatch.setattr(
        route_results, "_compute_refresh", lambda target: {"key": target}
    )

    class FinishedChild:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def run_child(argv):
        with monkeypatch.context() as env:
            env.setenv("RECKON_MOUNTS_PATH", "")
            assert route_results._refresh_main(argv[argv.index("--key") :]) == 0
        return FinishedChild()

    monkeypatch.setattr(route_results, "_spawn_refresh", run_child)
    assert route_results._start_refresh(key)
    payload = json.loads(route_results._refresh_store_path(key).read_text())
    assert payload["source_revision"] == revision
