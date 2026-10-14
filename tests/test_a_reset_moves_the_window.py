"""A reset moves a window end by a window, not by a stamp's minute of drift.

The provider's ``resets_at`` drifts by well over a minute while the window stays
where it is, so a stamp that moves that little names the same window. The stamp
signal a reset-anchored lift reads fires only on a move of at least half the
clock's window -- 3.5 days for ``seven_day``, 2.5 hours for ``five_hour`` -- the
span a genuine rollover carries. These tests grant a lift against a recorded
stamp and drive the polls the way the producer and the pace path each hand
readings over: the watch sweep hands one reading per poll, exactly as
``published_readings`` returns it, and a pace-path history hands the whole
sequence at once.

Every fixture points the config home and the watch stream at a temporary
directory and asserts afterwards that the live workstation's
``budget-lifts.json`` and watch stream are byte-identical, because a test that
writes the real records is a monitor for the machine rather than the code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_lift as bl
from reckon.crew import lift_watch, runs

GROUP = "codex-sub"
PROJECT = "reckon"
NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
ENV = {"USER": "lead"}

# The recorded stamp and the drifted one the live sample showed, 83 s apart.
RECORDED = datetime(2026, 10, 20, 4, 58, 15, tzinfo=UTC)
DRIFTED = datetime(2026, 10, 20, 4, 59, 38, tzinfo=UTC)

GRANT_FIGURE = 0.06
DRIFTED_FIGURE = 0.64


def _reading(clock: str, *, utilisation: float, resets_at: datetime) -> dict:
    """A window reading in the producer's shape, one clock populated."""
    return {
        "observed_at": NOW.isoformat(),
        clock: {"utilisation": utilisation, "resets_at": resets_at.isoformat()},
    }


def _config() -> dict:
    return {
        "backends": {GROUP: {"budget_group": GROUP}},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 0.0,
            "coordinator_reserve_pct": 3.0,
        },
    }


@pytest.fixture(autouse=True)
def _isolated_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
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
    readings: Sequence[dict],
    *,
    clock: str = bl.SEVEN_DAY,
    multiple: float = 1.8,
) -> dict:
    return bl.grant(
        config,
        group=GROUP,
        reason="spend the window",
        multiple=multiple,
        clock=clock,
        readings=list(readings),
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


class _Readings:
    """The group's reading, mutable between polls so a drift can be shown."""

    def __init__(self, reading: dict) -> None:
        self.reading = reading

    def __call__(self, group: str, config: object, now: datetime) -> list[dict]:
        return [self.reading]


def test_a_drifted_single_poll_leaves_the_lift_in_force():
    """One reading per poll, alternating stamps, never ends the lift."""
    config = _config()
    lift = _grant(
        config,
        [_reading(bl.SEVEN_DAY, utilisation=GRANT_FIGURE, resets_at=RECORDED)],
    )
    assert lift["ends"]["kind"] == "reset"
    assert lift["ends"]["resets_at"] == RECORDED.isoformat()
    assert lift["ends"]["utilisation"] == pytest.approx(GRANT_FIGURE)

    for stamp in (DRIFTED, RECORDED, DRIFTED):
        poll = _reading(bl.SEVEN_DAY, utilisation=DRIFTED_FIGURE, resets_at=stamp)
        assert (
            bl.end_cause(lift, readings=[poll], now=NOW, bound=bl.ceilings(config))
            is None
        )
        block = bl.effective_budget(config, group=GROUP, readings=[poll], now=NOW)
        assert block["lift_id"] == lift["id"]


def test_a_pace_path_history_leaves_the_lift_in_force():
    """A whole sequence whose newest stamp drifts 83 s stays in force.

    The measured jitter of the earlier readings is one second, so a resolver
    that reads the spread of the stored stamps ends the lift here.
    """
    config = _config()
    lift = _grant(
        config,
        [_reading(bl.SEVEN_DAY, utilisation=GRANT_FIGURE, resets_at=RECORDED)],
    )
    history = [
        _reading(bl.SEVEN_DAY, utilisation=DRIFTED_FIGURE, resets_at=RECORDED),
        _reading(bl.SEVEN_DAY, utilisation=DRIFTED_FIGURE, resets_at=RECORDED),
        _reading(bl.SEVEN_DAY, utilisation=DRIFTED_FIGURE, resets_at=DRIFTED),
    ]
    assert (
        bl.end_cause(lift, readings=history, now=NOW, bound=bl.ceilings(config)) is None
    )


def test_the_sweep_appends_no_row_for_a_drifted_stamp(tmp_path: Path):
    """The producer hands one reading per poll, and a drift writes no row."""
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(
        _reading(bl.SEVEN_DAY, utilisation=GRANT_FIGURE, resets_at=RECORDED)
    )
    _grant(config, [readings.reading])
    seen = lift_watch.sweep(
        PROJECT,
        seen={},
        now=NOW,
        config=config,
        readings_for=readings,
        stream_path=stream,
    )
    assert [row["to_state"] for row in _rows(stream)] == [lift_watch.LIFT_GRANTED]

    for stamp in (DRIFTED, RECORDED, DRIFTED):
        readings.reading = _reading(
            bl.SEVEN_DAY, utilisation=DRIFTED_FIGURE, resets_at=stamp
        )
        seen = lift_watch.sweep(
            PROJECT,
            seen=seen,
            now=NOW,
            config=config,
            readings_for=readings,
            stream_path=stream,
        )
    assert [row["to_state"] for row in _rows(stream)] == [lift_watch.LIFT_GRANTED]


def test_a_seven_day_move_ends_the_lift():
    config = _config()
    lift = _grant(
        config,
        [_reading(bl.SEVEN_DAY, utilisation=GRANT_FIGURE, resets_at=RECORDED)],
    )
    rolled = _reading(
        bl.SEVEN_DAY,
        utilisation=DRIFTED_FIGURE,
        resets_at=RECORDED + timedelta(days=7),
    )
    assert (
        bl.end_cause(lift, readings=[rolled], now=NOW, bound=bl.ceilings(config))
        == bl.END_RESET
    )
    assert (
        bl.effective_budget(config, group=GROUP, readings=[rolled], now=NOW)
        is config["budget"]
    )


def test_a_five_hour_lift_survives_a_three_minute_drift():
    config = _config()
    recorded = NOW + timedelta(hours=4)
    lift = _grant(
        config,
        [_reading(bl.FIVE_HOUR, utilisation=GRANT_FIGURE, resets_at=recorded)],
        clock=bl.FIVE_HOUR,
    )
    drifted = _reading(
        bl.FIVE_HOUR,
        utilisation=DRIFTED_FIGURE,
        resets_at=recorded + timedelta(minutes=3),
    )
    assert (
        bl.end_cause(lift, readings=[drifted], now=NOW, bound=bl.ceilings(config))
        is None
    )


def test_a_five_hour_move_ends_the_lift():
    config = _config()
    recorded = NOW + timedelta(hours=4)
    lift = _grant(
        config,
        [_reading(bl.FIVE_HOUR, utilisation=GRANT_FIGURE, resets_at=recorded)],
        clock=bl.FIVE_HOUR,
    )
    rolled = _reading(
        bl.FIVE_HOUR,
        utilisation=DRIFTED_FIGURE,
        resets_at=recorded + timedelta(hours=5),
    )
    assert (
        bl.end_cause(lift, readings=[rolled], now=NOW, bound=bl.ceilings(config))
        == bl.END_RESET
    )


def test_a_reading_below_the_grant_figure_still_ends_the_lift():
    config = _config()
    lift = _grant(
        config,
        [_reading(bl.SEVEN_DAY, utilisation=GRANT_FIGURE, resets_at=RECORDED)],
    )
    fell = _reading(bl.SEVEN_DAY, utilisation=0.03, resets_at=RECORDED)
    assert (
        bl.end_cause(lift, readings=[fell], now=NOW, bound=bl.ceilings(config))
        == bl.END_RESET
    )