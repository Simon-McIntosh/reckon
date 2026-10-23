"""An end once observed stays ended, so a flipping reading cannot revive a lift.

A reset-anchored lift's end is read from the group's window reading at the
moment of use, and nothing runs at the instant it ends. When two figures compete
and either could be the newest -- two members publishing different stamps, or a
utilisation read on either side of the figure recorded at grant -- the lift ends
on one read and is revived under the same id on the next, so the producer prints
``lift-granted`` and ``lift-ended`` for that id on every poll.

The first read that observes the end now stamps it on the lift record, and the
one end ladder reads that stamp ahead of every reading, so the id is granted
once and ended once. These tests drive the producer's own sweep,
``lift_watch.sweep``, through a sequence of readings that alternate between the
two figures, and read the record back to check the stamp. A steady sequence
leaves a lift that has not ended in force and its record unstamped, so the latch
records an observed end rather than ending a lift that never ended.

Every fixture points the config home at a temporary directory and asserts
afterwards that the live workstation's ``budget-lifts.json`` carries no trace of
the group this file mints, because a test that writes the real record is a
monitor for the machine rather than for the code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_lift as bl
from reckon.crew import lift_watch
from tests.real_home_traces import traces_naming

GROUP = "ended-lift-fixture"
PROJECT = "reckon"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ENV = {"USER": "lead"}

# The stamp the grant is anchored to, and the two utilisation figures a flipping
# reading alternates between: one above the figure recorded at grant, one below
# it, which is the signal that reads as an early limit reset.
RECORDED = NOW + timedelta(days=4)
GRANT_FIGURE = 0.60
ABOVE = 0.64
BELOW = 0.30

# The number of polls both sequences run for, so the steady case is measured
# over the same span as the flipping one.
POLLS = 5


def _reading(utilisation: float) -> dict:
    """A window reading in the shape the producer hands the reader."""
    return {
        "observed_at": NOW.isoformat(),
        bl.SEVEN_DAY: {"utilisation": utilisation, "resets_at": RECORDED.isoformat()},
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
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the config home at a temp dir; prove the real record is untouched."""
    home = Path.home() / ".config" / "reckon"
    before = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after = traces_naming(home, (bl.LIFTS_LEAF,), (GROUP,))
    assert after == before, f"a test wrote the live budget-lifts.json: {after}"


class _Readings:
    """The group's reading, mutable between polls so a flip can be shown.

    The sweep asks for one reading per poll, which is what
    ``published_readings`` returns: the group's freshest published window
    reading. Substituting it between polls is exactly the flip the nova stream
    shows, where two members take turns being the newest.
    """

    def __init__(self, reading: dict) -> None:
        self.reading = reading

    def __call__(self, group: str, config: object, now: datetime) -> list[dict]:
        return [self.reading]


def _grant(config: dict, readings: Sequence[dict]) -> dict:
    """Grant a reset-anchored lift anchored to ``readings``' seven-day stamp."""
    return bl.grant(
        config,
        group=GROUP,
        reason="run a build hard",
        multiple=1.8,
        clock=bl.SEVEN_DAY,
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


def _states(stream: Path) -> list[str]:
    return [row["to_state"] for row in _rows(stream)]


def _record(lift_id: str) -> dict:
    """The lift record as it stands on disk, read back by id."""
    document = bl.read_document()
    for lift in document["lifts"]:
        if lift.get("id") == lift_id:
            return lift
    raise AssertionError(f"lift {lift_id!r} is not on the record")


def _polls(
    config: dict,
    readings: _Readings,
    figures: Sequence[float],
    seen: dict,
    *,
    stream: Path,
) -> dict:
    """Run one sweep per figure, threading the producer's memory through."""
    for utilisation in figures:
        readings.reading = _reading(utilisation)
        seen = lift_watch.sweep(
            PROJECT,
            seen=seen,
            now=NOW,
            config=config,
            readings_for=readings,
            stream_path=stream,
        )
    return seen


def test_an_alternating_reading_grants_and_ends_the_lift_once(tmp_path: Path):
    """A reading that flips between two figures ends the lift exactly once.

    The figures alternate either side of the one recorded at grant, so the
    reader observes the reset on the low polls and does not on the high ones. At
    the base revision every high poll re-grants the id; with the end stamped the
    first time it is observed, the id is granted once and ended once.
    """
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(GRANT_FIGURE))
    lift = _grant(config, [readings.reading])

    seen = lift_watch.sweep(
        PROJECT,
        seen={},
        now=NOW,
        config=config,
        readings_for=readings,
        stream_path=stream,
    )
    assert _states(stream) == [lift_watch.LIFT_GRANTED]

    seen = _polls(
        config,
        readings,
        (BELOW, ABOVE, BELOW, ABOVE, BELOW),
        seen,
        stream=stream,
    )
    assert seen == {}, "the ended lift is still remembered as in force"

    states = _states(stream)
    assert states.count(lift_watch.LIFT_GRANTED) == 1
    assert states.count(lift_watch.LIFT_ENDED) == 1
    ended = [row for row in _rows(stream) if row["to_state"] == lift_watch.LIFT_ENDED]
    assert "cause reset" in ended[0]["detail"]

    record = _record(lift["id"])
    assert record["ended_at"] == NOW.isoformat()
    assert record["ended_cause"] == bl.END_RESET

    # And the pace path agrees the lift is over, even on a reading that alone
    # would put it back in force.
    assert (
        bl.effective_budget(config, group=GROUP, readings=[_reading(ABOVE)], now=NOW)
        is config["budget"]
    )


def test_a_steady_reading_keeps_the_lift_in_force(tmp_path: Path):
    """A lift whose end is never observed stays in force, and stays unstamped.

    The same number of readings as the flipping sequence, all on the in-force
    side of the grant figure, so the latch is shown to record an observed end
    rather than to end a lift the readings never ended.
    """
    config = _config()
    stream = tmp_path / "proj.events"
    readings = _Readings(_reading(GRANT_FIGURE))
    lift = _grant(config, [readings.reading])

    seen = lift_watch.sweep(
        PROJECT,
        seen={},
        now=NOW,
        config=config,
        readings_for=readings,
        stream_path=stream,
    )
    _polls(config, readings, [ABOVE] * POLLS, seen, stream=stream)

    assert _states(stream) == [lift_watch.LIFT_GRANTED]
    record = _record(lift["id"])
    assert "ended_at" not in record
    assert (
        bl.effective_budget(config, group=GROUP, readings=[readings.reading], now=NOW)[
            "lift_id"
        ]
        == lift["id"]
    )


def test_the_pace_path_stamps_the_end_it_observes(tmp_path: Path):
    """The budget read stamps the end too, so a flip cannot outrun the producer.

    The pace path resolves the group on every dispatch, independently of the
    producer's sweep. Whichever read observes the end first writes it, so the
    lift is ended for both readers from that moment.
    """
    config = _config()
    lift = _grant(config, [_reading(GRANT_FIGURE)])

    assert (
        bl.effective_budget(config, group=GROUP, readings=[_reading(BELOW)], now=NOW)
        is (config["budget"])
    )
    record = _record(lift["id"])
    assert record["ended_at"] == NOW.isoformat()
    assert record["ended_cause"] == bl.END_RESET

    # The latch reads ahead of the pace path's own reading, so a later in-force
    # reading no longer revives it.
    assert (
        bl.effective_budget(config, group=GROUP, readings=[_reading(ABOVE)], now=NOW)
        is config["budget"]
    )
