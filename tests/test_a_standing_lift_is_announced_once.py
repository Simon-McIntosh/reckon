"""A standing lift is drawn once, not re-announced on every watcher restart.

The producer sweeps the workstation's lifts onto the watch stream: one row when
a lift comes into force, one when it leaves, and, on its first pass after it
starts, a ``baseline`` row for every lift already in force. A producer restarts
on every reload, so without a memory a standing lift is announced again each
time. A baseline is inventory of what already exists, not something that
happened, so the pane draws it only the first time it sees the group.

These drive the real ``lift_watch.sweep`` — every row is the shape the stream
carries — and feed those rows through the follower's own row path, so a
measurement counts the rows the pane prints rather than a fixture's opinion of
them. Every fixture points the config home at a temporary directory and asserts
afterwards that the real lift record and the real watch stream are untouched.

The declared negative control — removing the row policy's hold on a baseline
lift row for a group the pane already drew — is applied on a scratch copy, not
here; these tests are the green arm, and that mutation must turn the restart
case red.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import budget_lift as bl
from reckon.crew import lift_watch, runs, ticker

WEEK_HOURS = bl.CLOCK_HOURS[bl.SEVEN_DAY]
GROUP = "codex-sub"
PROJECT = "reckon"
NOW = datetime(2026, 10, 8, 3, 12, tzinfo=UTC)
ENV = {"USER": "lead"}


def _reset(*, elapsed_hours: float) -> datetime:
    """The seven-day reset a group shows after ``elapsed_hours`` of its week."""
    return NOW + timedelta(hours=WEEK_HOURS - elapsed_hours)


def _reading(*, week_utilisation: float, elapsed_hours: float) -> dict:
    return {
        "observed_at": NOW.isoformat(),
        "five_hour": {
            "utilisation": 0.05,
            "resets_at": (NOW + timedelta(hours=4)).isoformat(),
        },
        "seven_day": {
            "utilisation": week_utilisation,
            "resets_at": _reset(elapsed_hours=elapsed_hours).isoformat(),
        },
    }


def _config(**budget: object) -> dict:
    block = {
        "pace_multiple": 1.1,
        "utilisation_ceiling_pct": 100.0,
        "resume_reserve_pct": 5.0,
        "coordinator_reserve_pct": 3.0,
    }
    block.update(budget)
    return {"backends": {GROUP: {"budget_group": GROUP}}, "budget": block}


class _Readings:
    """A group's reading, mutable between sweeps so a reset can be shown."""

    def __init__(self, reading: dict) -> None:
        self.reading = reading

    def __call__(self, group: str, config: object, now: datetime) -> list[dict]:
        return [self.reading]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the config home at a temp dir; prove the real records are untouched."""
    real_lifts = bl.lifts_path()
    real_stream = runs.watch_stream_path(PROJECT)
    before_lifts = real_lifts.read_bytes() if real_lifts.exists() else None
    before_stream = real_stream.read_bytes() if real_stream.exists() else None
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after_lifts = real_lifts.read_bytes() if real_lifts.exists() else None
    after_stream = real_stream.read_bytes() if real_stream.exists() else None
    assert after_lifts == before_lifts, "a test wrote the live budget-lifts.json"
    assert after_stream == before_stream, "a test wrote the live watch stream"


def _grant(config: dict, readings: list[dict], *, multiple: float = 1.8) -> dict:
    return bl.grant(
        config,
        group=GROUP,
        reason="spend the window",
        multiple=multiple,
        ends=None,
        readings=readings,
        granted_by="lead",
        now=NOW,
        environ=ENV,
    )


def _rows(stream: Path) -> list[dict]:
    if not stream.exists():
        return []
    return [
        json.loads(line) for line in stream.read_text().splitlines() if line.strip()
    ]


def _sweep(
    stream: Path,
    readings: _Readings,
    config: dict,
    *,
    seen: dict | None,
    now: datetime = NOW,
) -> dict:
    return lift_watch.sweep(
        PROJECT,
        seen=seen,
        now=now,
        config=config,
        readings_for=readings,
        stream_path=stream,
    )


def _printed(path: ticker.PaneRowPath, rows: Sequence[dict], *, start: float = 1000.0):
    """Feed rows to one pane in order and return the rows it printed."""
    out: list[dict] = []
    now = start
    for row in rows:
        out.extend(path.feed(row, now=now))
        now += 1.0
    return out


def _lift_row(
    group: str, *, kind: str = "transition", ended: str | None = None
) -> dict:
    """One lift row in the producer's own shape, for a named group."""
    lift: dict[str, Any] = {
        "group": group,
        "form": "five_hour",
        "pace_multiple": 1.8,
        "scope": "global",
        "id": f"{group}-lift",
    }
    return lift_watch._lift_row(PROJECT, lift, kind=kind, ended=ended)


# ── the restart re-announcement ─────────────────────────────────────────────


def test_a_restarted_watcher_adds_no_row_for_a_lift_the_pane_already_drew(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])

    # The watcher's first pass announces the grant, and the pane draws it.
    _sweep(stream, readings, config, seen={})
    grant_row = _rows(stream)[-1]
    assert grant_row["to_state"] == lift_watch.LIFT_GRANTED

    # The watcher restarts and loses its memory, so it re-derives a baseline for
    # the lift still standing — the row a reader saw after every producer reload.
    _sweep(stream, readings, config, seen=None)
    baseline_row = _rows(stream)[-1]
    assert baseline_row["event"] == "baseline"
    assert baseline_row["to_state"] == lift_watch.LIFT_IN_FORCE

    path = ticker.PaneRowPath()
    assert [row["to_state"] for row in _printed(path, [grant_row])] == [
        lift_watch.LIFT_GRANTED
    ]
    assert _printed(path, [baseline_row]) == [], (
        "a restarted watcher re-announces a lift the pane already drew"
    )


def test_a_pane_attaching_for_the_first_time_shows_the_standing_lift_once(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    _sweep(stream, readings, config, seen=None)
    baseline_row = _rows(stream)[-1]
    assert baseline_row["to_state"] == lift_watch.LIFT_IN_FORCE

    path = ticker.PaneRowPath()
    printed = _printed(path, [baseline_row])
    assert [row["to_state"] for row in printed] == [lift_watch.LIFT_IN_FORCE], (
        "a first attach must still show the standing lift"
    )


def test_a_grant_and_an_end_each_draw_one_row(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    seen = _sweep(stream, readings, config, seen={})
    grant_row = _rows(stream)[-1]
    assert grant_row["to_state"] == lift_watch.LIFT_GRANTED

    assert bl.clear(config, group=GROUP, now=NOW, environ=ENV) is not None
    _sweep(stream, readings, config, seen=seen)
    end_row = _rows(stream)[-1]
    assert end_row["to_state"] == lift_watch.LIFT_ENDED

    path = ticker.PaneRowPath()
    assert [row["to_state"] for row in _printed(path, [grant_row])] == [
        lift_watch.LIFT_GRANTED
    ]
    assert [row["to_state"] for row in _printed(path, [end_row])] == [
        lift_watch.LIFT_ENDED
    ]


def test_the_end_forgets_the_group_so_a_new_grant_draws_again(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    seen = _sweep(stream, readings, config, seen={})
    grant_row = _rows(stream)[-1]
    assert bl.clear(config, group=GROUP, now=NOW, environ=ENV) is not None
    _sweep(stream, readings, config, seen=seen)
    end_row = _rows(stream)[-1]

    path = ticker.PaneRowPath()
    _printed(path, [grant_row, end_row])

    # A fresh grant after the end is news again, because the end forgot the group.
    _grant(config, [readings.reading], multiple=2.0)
    _sweep(stream, readings, config, seen={})
    regrant_row = _rows(stream)[-1]
    assert regrant_row["to_state"] == lift_watch.LIFT_GRANTED
    assert [row["to_state"] for row in _printed(path, [regrant_row])] == [
        lift_watch.LIFT_GRANTED
    ]


# ── the memory is keyed on the lift's group ─────────────────────────────────


def test_a_baseline_for_a_group_the_pane_has_not_drawn_still_prints():
    """The hold is per group, so another group's standing lift still shows."""
    policy = ticker.RowPolicy()
    assert len(policy.feed(_lift_row("g-a"), now=1.0)) == 1  # a grant for g-a
    # A baseline for g-b is inventory the pane has not seen: it draws.
    assert len(policy.feed(_lift_row("g-b", kind="baseline"), now=2.0)) == 1
    # The pane has now drawn g-a, so g-a's own baseline draws nothing.
    assert policy.feed(_lift_row("g-a", kind="baseline"), now=3.0) == []


def test_a_lift_row_with_no_group_prints_rather_than_being_held():
    """An unkeyable row is not silently swallowed by a group-keyed memory."""
    policy = ticker.RowPolicy()
    assert len(policy.feed(_lift_row("", kind="baseline"), now=1.0)) == 1
    assert len(policy.feed(_lift_row("", kind="baseline"), now=2.0)) == 1
