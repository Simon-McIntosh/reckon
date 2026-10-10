"""The producer's lift sweep: one row when a lift starts, one when it ends.

Every figure is derived from the fixtures at assertion time, and every test
points the config home at a temporary directory, synthesises its own lift
document and stream, and asserts afterwards that the real ``budget-lifts.json``
and the real watch stream are untouched — a test that writes the live
workstation's lift record is a monitor for the machine rather than a test of the
code.

The declared negative control — removing ``lift-granted``, ``lift-ended`` and
``lift-in-force`` from ``ticker.CLAUSE_STATES`` so a lift row renders its state
word without its clause — is applied on a scratch copy, not here; these tests
are the green arm, and the mutation must fail the rendered-clause cases below.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_lift as bl
from reckon.crew import lift_watch, runs
from reckon.crew.recovery_watch import format_watch_transition
from reckon.crew.ticker import Ticker

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


def _grant(
    config: dict,
    readings: list[dict],
    *,
    multiple: float = 1.8,
    ends: dict | None = None,
) -> dict:
    return bl.grant(
        config,
        group=GROUP,
        reason="spend the window",
        multiple=multiple,
        ends=ends,
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


# The clause is the row's last column and is bounded by the pane: a lift row
# names its group, form, multiple, scope and id in that clause, so the pane the
# assertion reads through is wide enough to show it rather than a default grid
# that would cut it to a recognisable head — a pane that cannot show a naming
# clause proves nothing about whether the row carries one.
_WIDE = 400


def _rendered(row: dict) -> tuple[str, str]:
    """A row as both followers render it: the formatter and the ticker grid."""
    return (
        format_watch_transition(row, ticker=Ticker(width=_WIDE)),
        Ticker(width=_WIDE).render(row),
    )


# ── the grant row ───────────────────────────────────────────────────────────


def test_a_grant_writes_exactly_one_row_and_an_unchanged_sweep_writes_none(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    lift = _grant(config, [readings.reading])

    seen = _sweep(stream, readings, config, seen={})
    rows = _rows(stream)
    assert len(rows) == 1, "a grant writes exactly one row"
    row = rows[0]
    assert row["event"] == "transition"
    assert row["to_state"] == lift_watch.LIFT_GRANTED
    detail = str(row["detail"])
    for named in (
        GROUP,
        lift["form"],
        str(lift["pace_multiple"]),
        lift["scope"],
        lift["id"],
    ):
        assert str(named) in detail, f"the grant row names {named!r}"

    _sweep(stream, readings, config, seen=seen)
    assert _rows(stream) == rows, "a sweep with nothing changed writes none"


def test_a_granted_row_renders_its_naming_clause_through_both_followers(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    lift = _grant(config, [readings.reading])
    _sweep(stream, readings, config, seen={})

    row = _rows(stream)[0]
    assert row["to_state"] == lift_watch.LIFT_GRANTED
    for rendered in _rendered(row):
        for named in (
            GROUP,
            lift["form"],
            str(lift["pace_multiple"]),
            lift["id"],
        ):
            assert str(named) in rendered, (
                f"the rendered grant line names {named!r}: {rendered!r}"
            )


def test_a_reload_marks_a_lift_already_in_force_as_baseline(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])

    _sweep(stream, readings, config, seen=None)
    rows = _rows(stream)
    assert len(rows) == 1
    assert rows[0]["event"] == "baseline", "a re-armed producer announces no new lift"
    assert rows[0]["to_state"] == lift_watch.LIFT_IN_FORCE


def test_the_rows_render_through_both_followers(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    seen = _sweep(stream, readings, config, seen={})
    assert bl.clear(config, group=GROUP, now=NOW, environ=ENV) is not None
    _sweep(stream, readings, config, seen=seen)

    rows = _rows(stream)
    assert len(rows) == 2
    for row in rows:
        assert isinstance(format_watch_transition(row), str)
        assert isinstance(Ticker().render(row), str)


# ── the four ways a lift ends ──────────────────────────────────────────────


def test_a_moved_reset_ends_the_lift(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    seen = _sweep(stream, readings, config, seen={})

    readings.reading = _reading(week_utilisation=0.21, elapsed_hours=0.0)
    _sweep(stream, readings, config, seen=seen)

    rows = _rows(stream)
    assert len(rows) == 2
    assert rows[-1]["to_state"] == lift_watch.LIFT_ENDED
    assert lift_watch.END_RESET in str(rows[-1]["detail"])


def test_a_passed_at_ends_the_lift(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    at = NOW + timedelta(hours=2)
    _grant(config, [readings.reading], ends={"kind": "at", "at": at.isoformat()})
    seen = _sweep(stream, readings, config, seen={})

    _sweep(stream, readings, config, seen=seen, now=at + timedelta(seconds=1))
    rows = _rows(stream)
    assert len(rows) == 2
    assert lift_watch.END_AT in str(rows[-1]["detail"])


def test_the_hard_ceiling_ends_the_lift(tmp_path):
    config = _config(lift={"max_hours": 1.0})
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    seen = _sweep(stream, readings, config, seen={})

    _sweep(stream, readings, config, seen=seen, now=NOW + timedelta(hours=2))
    rows = _rows(stream)
    assert len(rows) == 2
    assert lift_watch.END_CEILING in str(rows[-1]["detail"])


def test_a_clear_ends_the_lift(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    seen = _sweep(stream, readings, config, seen={})

    assert bl.clear(config, group=GROUP, now=NOW, environ=ENV) is not None
    _sweep(stream, readings, config, seen=seen)
    rows = _rows(stream)
    assert len(rows) == 2
    assert rows[-1]["to_state"] == lift_watch.LIFT_ENDED
    assert lift_watch.END_CLEAR in str(rows[-1]["detail"])


def test_an_ended_row_renders_its_cause_through_both_followers(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    _grant(config, [readings.reading])
    seen = _sweep(stream, readings, config, seen={})
    assert bl.clear(config, group=GROUP, now=NOW, environ=ENV) is not None
    _sweep(stream, readings, config, seen=seen)

    row = _rows(stream)[-1]
    assert row["to_state"] == lift_watch.LIFT_ENDED
    for rendered in _rendered(row):
        assert lift_watch.END_CLEAR in rendered, (
            f"the rendered ended line names its cause: {rendered!r}"
        )


# ── the reading, and the untouched record ─────────────────────────────────


def test_the_in_force_verdict_is_read_through_budget_lift(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    lift = _grant(config, [readings.reading])
    assert bl._in_force(
        lift,
        readings=[readings.reading],
        now=NOW,
        bound=bl.ceilings(config),
    ), "the fixture lift starts in force"
    seen = _sweep(stream, readings, config, seen={})
    assert set(seen) == {lift["id"]}
    assert len(_rows(stream)) == 1


def test_no_lift_in_force_writes_no_row(tmp_path):
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(week_utilisation=0.21, elapsed_hours=22.0))
    assert bl.read_document()["lifts"] == []
    seen = _sweep(stream, readings, config, seen={})
    assert _rows(stream) == [], "no lift in force writes no row"
    assert seen == {}, "no lift in force leaves the memory empty"
