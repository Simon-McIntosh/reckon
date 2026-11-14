"""A limit reset reaches every station-fed pane, once.

The lead's reset announcement was folded into each project's producer stream,
and only there. Panes now read the station's own events, which never carried
it, so on 14 November the reset was recorded, counted as reaching seventeen
sessions, and drawn on none of their panes. The station now publishes each due
reset into every project's events, and remembers what it announced so a later
tick, or a restarted station, does not announce it again.
"""

from __future__ import annotations

from datetime import timedelta

from reckon.crew import budget_reset, lift_watch, station, station_store
from tests import test_a_phase_transition_is_published_before_review_duties as base

FIRST, SECOND, MOMENT = base.FIRST, base.SECOND, base.MOMENT
mounted = base.mounted


def _station(root, crew_home_dir) -> station.Station:
    store = station_store.StationStore(root)
    beat = station.SweepBeat(sweep=base._Sweeps(), start=lambda action: action())
    return station.Station(
        store,
        home=crew_home_dir,
        projects=lambda: {FIRST: "", SECOND: ""},
        sweeps=beat,
    )


def _reset_rows(node: station.Station) -> list[dict]:
    return [
        event
        for event in node.store.events()
        if event.get("to_state") == lift_watch.LIMIT_RESET
    ]


def test_a_due_reset_is_published_to_every_project_once(home, tmp_path, mounted):
    budget_reset.mark_used(
        "claude-orchestrators",
        by="lead",
        moment=MOMENT - timedelta(minutes=2),
        reason="limits reset, continue",
        clock="five_hour",
    )
    root = tmp_path / "station"
    node = _station(root, home)
    node.tick(now=MOMENT)
    rows = _reset_rows(node)
    assert sorted(row.get("project") for row in rows) == [FIRST, SECOND], rows
    assert all(row.get("audience") == "*" for row in rows), rows

    node.tick(now=MOMENT + timedelta(seconds=5))
    assert len(_reset_rows(node)) == 2

    restarted = _station(root, home)
    restarted.tick(now=MOMENT + timedelta(seconds=10))
    assert len(_reset_rows(restarted)) == 2
