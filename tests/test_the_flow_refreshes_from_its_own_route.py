"""The flow refreshes from its own route, anchored at each request's instant.

``GET /_schedule/<project>`` answers the project's schedule alone, built the
way discovery builds it — from the stored inventory and the live crew rows at
the instant of the request. Every bar is placed relative to that instant, so
two requests a few minutes apart place the same in-flight bar differently, and
a run that goes live between them draws in flight. A crew-row read that fails
answers a null schedule beside its error rather than failing the route.

Each case carries its own control: the same request answered from a schedule
built at a single, earlier instant, or with the failure let through, so the
assertion that reads the real answer is shown to be able to fail.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import metadata_index, serve

PROJECT = "sample"

#: Two instants a few minutes apart, which is the window a crew poll spans.
INSTANT_ONE = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
INSTANT_TWO = datetime(2026, 10, 21, 12, 7, 0, tzinfo=UTC)

#: A live pointer for the one plan, in a phase the fleet reads as live.
LIVE_RUN = {
    "project": PROJECT,
    "plan": "alpha",
    "run_id": "r-alpha",
    "phase": "working",
}


def _plan_page(slug: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<title>{title}</title></head><body></body></html>
"""


def _mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary project whose one plan is active, mounted in this process."""

    docs = tmp_path / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    (plans / "alpha.html").write_text(_plan_page("alpha", "Alpha"), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(serve, "load_mounts", lambda: {PROJECT: docs})
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    metadata_index.clear()
    serve._DISC_CACHE.clear()
    return docs


class _FixedClock(datetime):
    """A datetime whose ``now`` is the instant the case supplies."""

    instant = INSTANT_ONE

    @classmethod
    def now(cls, tz=None):
        return cls.instant if tz is None else cls.instant.astimezone(tz)


def _freeze(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Anchor every schedule this process builds for the next request at ``instant``."""

    monkeypatch.setattr(
        serve,
        "datetime",
        type("FrozenDatetime", (_FixedClock,), {"instant": instant}),
    )


@contextlib.contextmanager
def _serving() -> Iterator[int]:
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    # Daemonic handler threads are not joined by server_close(), so a stream
    # handler could outlive the case and call into the next one's patches.
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(port: int, path: str) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _schedule(port: int) -> dict:
    """The served schedule, asserting the route answered one."""

    status, payload = _get(port, f"/_schedule/{PROJECT}")
    assert status == 200, (status, payload)
    assert payload.get("schedule") is not None, payload
    return payload["schedule"]


def _alpha(schedule: dict) -> dict:
    return next(bar for bar in schedule["bars"] if bar["nav_key"] == "alpha")


def _split_instant(schedule: dict) -> datetime:
    """The instant the bar's done share reaches: reference plus its split hours.

    Every figure the schedule serves is hours from its own ``reference``, so a
    split read on its own says nothing about which instant it sits at; adding
    the reference back is what makes the anchor checkable.
    """

    reference = datetime.fromisoformat(schedule["reference"])
    return reference + timedelta(hours=_alpha(schedule)["split"])


def test_the_route_places_an_in_flight_bar_at_each_requests_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests at two instants anchor the same bar at each of them.

    The in-flight bar's done share sits at the instant the request was served,
    so the split of a bar is a fact about the request and not about the
    process's uptime.
    """

    _mount(tmp_path, monkeypatch)
    monkeypatch.setattr(
        serve, "_crew_rows", lambda mounts, project=None: [dict(LIVE_RUN)]
    )

    with _serving() as port:
        _freeze(monkeypatch, INSTANT_ONE)
        first = _schedule(port)
        _freeze(monkeypatch, INSTANT_TWO)
        second = _schedule(port)
        # Control: the same two requests, both answered from one schedule built
        # at the first instant. Both are the route's own answer; only the
        # anchor differs, which is what the assertions above read.
        _freeze(monkeypatch, INSTANT_ONE)
        control_first = _schedule(port)
        control_second = _schedule(port)

    assert _alpha(first)["state"] == "in-flight", first
    assert _alpha(second)["state"] == "in-flight", second
    assert _split_instant(first) == INSTANT_ONE, first["reference"]
    assert _split_instant(second) == INSTANT_TWO, second["reference"]

    # The control carries whatever the two anchors agree on, and no more: its
    # second answer is the first instant, so the assertion above would fail had
    # the route built one schedule and served it twice.
    assert _split_instant(control_second) == _split_instant(control_first)
    assert _split_instant(control_second) != INSTANT_TWO


def test_a_run_going_live_between_requests_draws_its_bar_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that appears between two requests turns its plan's bar in flight.

    The schedule is rebuilt per request from the live crew rows, so a run that
    went live after the first answer is drawn in flight on the second; a
    schedule built before it went live keeps the bar predicted, which is the
    control this case is made on.
    """

    _mount(tmp_path, monkeypatch)
    live: list[dict] = []
    monkeypatch.setattr(serve, "_crew_rows", lambda mounts, project=None: list(live))

    with _serving() as port:
        _freeze(monkeypatch, INSTANT_ONE)
        before = _schedule(port)
        live.append(dict(LIVE_RUN))
        _freeze(monkeypatch, INSTANT_TWO)
        after = _schedule(port)

    assert _alpha(before)["state"] == "predicted", before
    assert _alpha(after)["state"] == "in-flight", after
    # The two answers are the same plan's bar, so the state is the only thing
    # that moved — and `before` is exactly the control: a schedule built before
    # the run went live, whose bar stays predicted.
    assert _alpha(before)["nav_key"] == _alpha(after)["nav_key"]


def test_a_crew_row_failure_answers_a_null_schedule_with_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crew-row read that fails costs the schedule and nothing else.

    The stored inventory is already in hand, so the route keeps its 200 and
    serves the failure beside a null schedule. Let through, the same failure
    takes the whole answer, which is the control.
    """

    _mount(tmp_path, monkeypatch)

    def unavailable(mounts, project=None):
        raise RuntimeError("crew rows unavailable")

    monkeypatch.setattr(serve, "_crew_rows", unavailable)

    with _serving() as port:
        _freeze(monkeypatch, INSTANT_ONE)
        status, payload = _get(port, f"/_schedule/{PROJECT}")
        # Control: the same failure let through rather than answered as a null
        # schedule, which cannot produce a schedule at all.
        monkeypatch.setattr(serve, "_attach_schedule_for_read", serve._attach_schedule)
        let_through, _ = _get(port, f"/_schedule/{PROJECT}")

    assert status == 200, payload
    assert payload["schedule"] is None, payload
    assert payload["schedule_error"] == "RuntimeError: crew rows unavailable", payload
    assert let_through == 500, (
        "the control did not answer 500, so the failure arm proves nothing"
    )
    assert status != let_through
