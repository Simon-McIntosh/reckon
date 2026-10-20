"""A schedule failure leaves discovery served.

The schedule is derived per read from the live crew rows, so its derivation can
fail while the stored inventory is already in hand. A failure attaching it must
not fail the read: the response carries the inventory with ``schedule`` null and
the failure named in ``schedule_error``, and the server logs it once. A failure
reading the stored result itself is a different matter and still answers 500.

``RECKON_SCHEDULE_ATTACH_FAILS_READ=1`` applies the declared mutation — the
schedule attach is put back inside the handler's error path, so a crew-row
failure answers 500 and the first case fails.
"""

from __future__ import annotations

import os

import pytest

from reckon import route_results, serve
from tests.test_the_slow_routes_answer_from_stored_results import PROJECT
from tests.test_the_slow_routes_answer_from_stored_results import served as _served

#: The declared mutation: the schedule attach shares the stored-result read's
#: error path, so a failure attaching the schedule fails the whole read.
_MUTATION = "RECKON_SCHEDULE_ATTACH_FAILS_READ"

CREW_FAILURE = "crew rows unavailable"
_ATTACH_MARKER = "without its schedule"
#: The logger record the read emits is filtered on this marker, so a record
#: from any other source does not count toward the once-per-read bound.
_SERVE_LOGGER = "reckon.serve"


@pytest.fixture
def served(tmp_path, monkeypatch):
    yield from _served.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def mutated(monkeypatch):
    if os.environ.get(_MUTATION):
        _fuse_attach_into_the_error_path(monkeypatch)


def _fuse_attach_into_the_error_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation: no degradation, the attach raises to the read."""

    monkeypatch.setattr(
        serve,
        "_attach_schedule_for_read",
        lambda result, project, mounts, reference: serve._attach_schedule(
            result, project, mounts, reference=reference
        ),
    )


def _store_inventory() -> None:
    key = ("discover", PROJECT)
    route_results._store_refresh_result(key, route_results._compute_refresh(key))


@pytest.fixture
def raising_crew_rows(monkeypatch):
    def boom(_mounts, _project):
        raise RuntimeError(CREW_FAILURE)

    monkeypatch.setattr(serve, "_crew_rows", boom)


def test_a_crew_row_failure_leaves_discovery_served(
    served, monkeypatch, raising_crew_rows, caplog
):
    _store_inventory()
    with caplog.at_level("WARNING", logger=_SERVE_LOGGER):
        caplog.clear()
        status, body, _ = served.get(f"/_discover/{PROJECT}")

    assert status == 200
    assert body["inventory"]
    assert body["schedule"] is None
    assert CREW_FAILURE in body["schedule_error"]

    named = [r for r in caplog.records if _ATTACH_MARKER in r.getMessage()]
    assert len(named) == 1, f"the read logged {len(named)} schedule failures"
    assert CREW_FAILURE in named[0].getMessage()


def test_a_later_read_carries_the_schedule_again(
    served, monkeypatch, raising_crew_rows, caplog
):
    _store_inventory()
    with caplog.at_level("WARNING", logger=_SERVE_LOGGER):
        status, body, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200
    assert body["schedule"] is None

    monkeypatch.setattr(serve, "_crew_rows", lambda _mounts, _project: [])
    status, body, _ = served.get(f"/_discover/{PROJECT}")
    assert status == 200
    assert body["schedule"] is not None
    assert body.get("schedule_error") is None


def test_a_stored_result_read_failure_still_answers_500(served, monkeypatch):
    def boom(_key, **_kwargs):
        raise RuntimeError("stored result unreadable")

    monkeypatch.setattr(route_results, "_served_result", boom)
    status, body, _ = served.get(f"/_discover/{PROJECT}")

    assert status == 500
    assert body["error"] == "distributed_project_state_invalid"


if __name__ == "__main__":  # pragma: no cover - manual launch
    raise SystemExit(pytest.main([__file__]))
