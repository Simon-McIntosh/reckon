"""Discovery keeps inventory at rest and derives its schedule for each read."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from reckon import route_results, serve
from tests.test_the_slow_routes_answer_from_stored_results import (
    PROJECT,
)
from tests.test_the_slow_routes_answer_from_stored_results import (
    served as _served,
)

STORE_TIME = datetime(2026, 10, 20, 6, 8, tzinfo=UTC)
READ_TIME = STORE_TIME + timedelta(minutes=27)


@pytest.fixture
def served(tmp_path, monkeypatch):
    yield from _served.__wrapped__(tmp_path, monkeypatch)


def _clock(reference: datetime):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return reference.astimezone(tz) if tz else reference.replace(tzinfo=None)

    return Clock


def _stored_inventory(served, monkeypatch):
    key = ("discover", PROJECT)
    store_schedule = bool(os.environ.get("RECKON_SCHEDULE_AT_STORE_TIME"))
    if store_schedule:
        original = route_results._compute_refresh

        def attach_at_store(target):
            result = original(target)
            return serve._attach_schedule(
                result, PROJECT, served.mounts, reference=STORE_TIME
            )

        monkeypatch.setattr(route_results, "_compute_refresh", attach_at_store)
    result = route_results._compute_refresh(key)
    route_results._store_refresh_result(key, result)
    if store_schedule:
        monkeypatch.setattr(
            serve, "_attach_schedule", lambda result, *_args, **_kwargs: result
        )
    return route_results._refresh_result(key)[0]


def test_refresh_child_stores_inventory_without_schedule(served, monkeypatch):
    stored = _stored_inventory(served, monkeypatch)
    assert stored["inventory"]
    assert "schedule" not in stored


def test_discovery_schedule_uses_each_read_instant_and_current_runs(
    served, monkeypatch
):
    _stored_inventory(served, monkeypatch)
    monkeypatch.setattr(serve, "datetime", _clock(STORE_TIME))
    monkeypatch.setattr(serve, "_crew_rows", lambda _mounts, _project: [])
    status, first, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200

    run = {"project": PROJECT, "plan": "alpha", "phase": "working"}
    monkeypatch.setattr(serve, "_crew_rows", lambda _mounts, _project: [run])
    monkeypatch.setattr(serve, "datetime", _clock(READ_TIME))
    status, second, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200
    bars = {bar["plan"]: bar for bar in second["schedule"]["bars"]}
    assert bars["alpha"]["state"] == "in-flight"
    assert first["schedule"]["reference"] == STORE_TIME.isoformat()
    assert second["schedule"]["reference"] == READ_TIME.isoformat()


def test_inline_discovery_attaches_the_schedule_on_its_read(served, monkeypatch):
    monkeypatch.setattr(route_results, "_REFRESH_ENABLED", False)
    monkeypatch.setattr(serve, "datetime", _clock(READ_TIME))
    monkeypatch.setattr(
        serve,
        "_crew_rows",
        lambda _mounts, _project: [
            {"project": PROJECT, "plan": "alpha", "phase": "working"}
        ],
    )

    status, result, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200
    assert result["schedule"]["reference"] == READ_TIME.isoformat()
    bars = {bar["plan"]: bar for bar in result["schedule"]["bars"]}
    assert bars["alpha"]["state"] == "in-flight"
