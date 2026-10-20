"""A lift that ends inside its window re-anchors the default pace hold.

A lift lets a group spend ahead of pace. When it stops before the window it was
anchored to resets -- cleared, or lapsed at its stated end or its hard ceiling --
the utilisation it spent stays in the burn ratio the pace hold compares, so a
group that stood at 75% with only 4% of its week gone holds every non-bookend
dispatch although a quarter of the week's budget remains. The remedy is no
second lift: the lift's end is stamped with the group's utilisation there, and
the default hold then compares against the line that spreads the remaining
budget evenly from that point to the reset.

Every figure below is derived from one fixture at assertion time. Each test
points ``RECKON_HOME`` at a temporary directory and asserts the workstation's
own ``budget-lifts.json`` is byte-identical before and after, because a test
that writes the live lift record is a monitor for the machine rather than a test
of the code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget
from reckon.crew import budget_lift as bl
from reckon.crew import paid_lanes, window_reading

GROUP = "re-anchor-fixture"
BACKEND = "codex-sub"
MULTIPLE = 1.1
WEEK_MINUTES = 10_080
WEEK_HOURS = 168.0

# The shape the brief records: the window ran 4% of its week when the lift
# stopped, and the group stood at 75% used.
ELAPSED_FRACTION = 0.04
U_END = 0.75


def _config() -> dict:
    return {
        "backends": {
            BACKEND: {"launch": "cli", "command": "codex", "budget_group": GROUP}
        },
        "budget": {
            "pace_multiple": MULTIPLE,
            "lift": {"max_multiple": 3.0, "max_hours": 168.0},
            "utilisation_ceiling_pct": 0.0,
            "resume_reserve_pct": 0.0,
            "coordinator_reserve_pct": 0.0,
        },
    }


class _Fixture:
    """One window whose start places the elapsed fraction the brief records."""

    def __init__(self, reset: datetime, end: datetime):
        self.reset = reset
        self.end = end

    @property
    def window_start(self) -> datetime:
        return self.reset - timedelta(hours=WEEK_HOURS)

    def line(self, moment: datetime, *, u_end: float = U_END) -> float:
        """The re-anchored allowance at ``moment``, from the fixture's own end."""
        span = (self.reset - self.end).total_seconds()
        share = (moment - self.end).total_seconds() / span
        return min(1.0, u_end + MULTIPLE * (1.0 - u_end) * share)


@pytest.fixture()
def fixture() -> _Fixture:
    # A window that still has most of its week left when the lift stops.
    reset = datetime(2026, 10, 27, 6, 32, tzinfo=UTC)
    end = (
        reset
        - timedelta(hours=WEEK_HOURS)
        + timedelta(hours=WEEK_HOURS * ELAPSED_FRACTION)
    )
    return _Fixture(reset, end)


_REAL_LIFTS = Path.home() / ".config" / "reckon" / bl.LIFTS_LEAF


def _real_lifts_bytes() -> bytes:
    try:
        return _REAL_LIFTS.read_bytes()
    except OSError:
        return b""


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the config home at a temp dir; leave the real lift record alone."""
    before = _real_lifts_bytes()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    yield tmp_path
    after = _real_lifts_bytes()
    assert after == before, "a test wrote the workstation's budget-lifts.json"


def _publish(
    home: Path,
    *,
    observed: datetime,
    utilisation: float,
    reset: datetime,
) -> None:
    """Write the published headroom document the group's reading is read from."""
    document = {
        "accounts": {
            BACKEND: {
                "windows": {
                    "seven_day": {
                        "state": paid_lanes.OBSERVED,
                        "utilisation": utilisation,
                        "observed_at": observed.isoformat(),
                        "resets_at": reset.isoformat(),
                    }
                }
            }
        }
    }
    (home / paid_lanes.DOCUMENT_NAME).write_text(json.dumps(document))


def _reading(
    *,
    utilisation: float,
    observed: datetime,
    reset: datetime,
) -> dict:
    """A reading in the shape ``budget_lift`` takes, one clock of interest."""
    return {
        "observed_at": observed.isoformat(),
        "five_hour": {
            "utilisation": 0.05,
            "resets_at": (observed + timedelta(hours=1)).isoformat(),
        },
        "seven_day": {
            "utilisation": utilisation,
            "resets_at": reset.isoformat(),
        },
    }


def _window(
    *,
    utilisation: float,
    observed: datetime,
    reset: datetime,
) -> window_reading.WindowReading:
    """The group's window reading as ``group_pace`` takes it."""
    figures = (
        window_reading.WindowFigure(
            period="five_hour",
            utilisation=0.05,
            observed_at=observed,
            age_seconds=5.0,
            resets_at=budget._iso(observed + timedelta(hours=1)),
            window_minutes=300,
        ),
        window_reading.WindowFigure(
            period="seven_day",
            utilisation=utilisation,
            observed_at=observed,
            age_seconds=5.0,
            resets_at=budget._iso(reset),
            window_minutes=WEEK_MINUTES,
        ),
    )
    return window_reading.WindowReading(
        figures=figures, observed_at=observed, age_seconds=5.0
    )


def _entry(config: dict, *, now: datetime, utilisation: float, reset: datetime) -> dict:
    """The single group entry ``group_pace`` emits for the fixture group."""
    report = budget.group_pace(
        config,
        windows={BACKEND: _window(utilisation=utilisation, observed=now, reset=reset)},
        now=now,
    )
    found = [entry for entry in report if entry["group"] == GROUP]
    assert len(found) == 1, f"expected one entry for {GROUP!r}, got {found}"
    return found[0]


def _grant_and_clear(fixture: _Fixture, config: dict) -> str:
    """Grant a lift and clear it at the fixture's end, at 75% used.

    The cleared lift carries the group's utilisation at the moment it stopped,
    read from the published document rather than handed in, so the path under
    test is the one a real clear takes.
    """
    _publish(
        bl.lifts_path().parent,
        observed=fixture.end,
        utilisation=U_END,
        reset=fixture.reset,
    )
    reading = _reading(utilisation=U_END, observed=fixture.end, reset=fixture.reset)
    lift = bl.grant(
        config,
        group=GROUP,
        reason="spend ahead into the window",
        form=bl.UNCAPPED,
        clock=bl.SEVEN_DAY,
        readings=[reading],
        now=fixture.end - timedelta(minutes=30),
        environ={"USER": "lead"},
    )
    cleared = bl.clear(
        config,
        group=GROUP,
        lift_id=lift["id"],
        now=fixture.end,
        environ={"USER": "lead"},
    )
    assert cleared is not None
    assert cleared["u_end"] == pytest.approx(U_END)
    assert datetime.fromisoformat(cleared["t_end"]) == fixture.end
    return lift["id"]


def test_a_cleared_lift_re_anchors_the_hold_at_its_end_utilisation(
    fixture: _Fixture,
) -> None:
    """A cleared lift at 75% with 4% elapsed admits at its line and holds above."""
    config = _config()
    _grant_and_clear(fixture, config)

    at_end = _entry(config, now=fixture.end, utilisation=U_END, reset=fixture.reset)
    anchor = at_end["allowance"]["re_anchor"]
    assert anchor["u_end"] == pytest.approx(U_END)
    assert anchor["t_end"] == budget._iso(fixture.end)
    assert anchor["allowance"] == pytest.approx(fixture.line(fixture.end))

    # The window is too young to project an elapsed-fraction burn, so the plain
    # comparison has nothing to judge with; the re-anchored line is what decides.
    verdict = budget.pace_hold(at_end, "implement")
    assert verdict["held"] is False
    assert verdict["allowance"] is None

    below = _entry(
        config, now=fixture.end, utilisation=U_END - 0.05, reset=fixture.reset
    )
    assert budget.pace_hold(below, "implement")["held"] is False

    above = _entry(
        config, now=fixture.end, utilisation=U_END + 0.01, reset=fixture.reset
    )
    above_verdict = budget.pace_hold(above, "implement")
    assert above_verdict["held"] is True
    assert "75%" in above_verdict["reason"]
    assert budget._iso(fixture.end) in above_verdict["reason"]


def test_the_re_anchored_line_rises_toward_the_reset(fixture: _Fixture) -> None:
    """The allowance spreads the remaining budget evenly from the group's end."""
    config = _config()
    _grant_and_clear(fixture, config)

    halfway = fixture.end + (fixture.reset - fixture.end) / 2
    entry = _entry(config, now=halfway, utilisation=0.80, reset=fixture.reset)
    anchor = entry["allowance"]["re_anchor"]
    assert anchor["allowance"] == pytest.approx(fixture.line(halfway))
    # At the halfway point the line stands above where it did at the end, and
    # the burn comparison the group would otherwise meet is unchanged.
    assert anchor["allowance"] > U_END
    assert budget.pace_hold(entry, "implement")["held"] is False


def test_a_lapsed_lift_is_stamped_once_across_two_reads(
    fixture: _Fixture,
) -> None:
    """A stated end inside the window is stamped once, by the first read."""
    config = _config()
    stated_end = fixture.end
    reading = _reading(utilisation=0.60, observed=stated_end, reset=fixture.reset)
    lift = bl.grant(
        config,
        group=GROUP,
        reason="spend until the stated end",
        form=bl.UNCAPPED,
        clock=bl.SEVEN_DAY,
        ends={"kind": "at", "at": budget._iso(stated_end)},
        readings=[reading],
        now=stated_end - timedelta(hours=1),
        environ={"USER": "lead"},
    )
    lifts_file = bl.lifts_path()

    bl.effective_budget(
        config,
        group=GROUP,
        readings=[reading],
        now=stated_end + timedelta(minutes=1),
    )
    after_first = lifts_file.read_bytes()
    stamped = next(
        item for item in bl.list_lifts(group=GROUP) if item["id"] == lift["id"]
    )
    assert stamped["u_end"] == pytest.approx(0.60)
    assert datetime.fromisoformat(stamped["t_end"]) == stated_end

    bl.effective_budget(
        config,
        group=GROUP,
        readings=[_reading(utilisation=0.61, observed=stated_end, reset=fixture.reset)],
        now=stated_end + timedelta(hours=1),
    )
    assert lifts_file.read_bytes() == after_first, "the lapse was stamped twice"


def test_a_window_that_reset_reverts_to_the_plain_comparison(
    fixture: _Fixture,
) -> None:
    """A lift that ended in an earlier window leaves no re-anchor behind."""
    config = _config()
    _grant_and_clear(fixture, config)

    moved = fixture.reset + timedelta(days=7)
    halfway = moved - timedelta(hours=WEEK_HOURS / 2)
    entry = _entry(config, now=halfway, utilisation=0.60, reset=moved)

    assert "re_anchor" not in entry["allowance"]
    # The plain comparison stands: 0.60 over half the window is 1.2x burn,
    # above the 1.1x multiple, so the dispatch is held as it always was.
    assert entry["allowance"]["burn_multiple"] > MULTIPLE
    assert budget.pace_hold(entry, "implement")["held"] is True


def test_a_group_that_never_carried_a_lift_is_unchanged(
    fixture: _Fixture,
) -> None:
    """No lift in the record leaves the hold exactly as it was."""
    config = _config()
    # The plan's own fixture: 21% of a 168 h window spent after 22.0 h.
    now = fixture.window_start + timedelta(hours=22.0)
    entry = _entry(config, now=now, utilisation=0.21, reset=fixture.reset)

    assert "re_anchor" not in entry["allowance"]
    assert entry["allowance"]["burn_multiple"] > MULTIPLE
    assert budget.pace_hold(entry, "implement")["held"] is True


def _write_cleared_lift(fixture: _Fixture, *, cleared_at: datetime) -> Path:
    """Write a lift record that carries a clear and no end stamp.

    This is the shape a record written by a build that predates the stamp, or by
    a clear superseded by a successor, is left in: the clear is there and the
    end point is not.
    """
    lifts = bl.lifts_path()
    lifts.parent.mkdir(parents=True, exist_ok=True)
    lifts.write_text(
        json.dumps(
            {
                "version": 1,
                "lifts": [
                    {
                        "id": "cleared-before-the-stamp",
                        "group": GROUP,
                        "scope": bl.GLOBAL,
                        "form": bl.UNCAPPED,
                        "granted_at": budget._iso(fixture.window_start),
                        "granted_by": "lead",
                        "cleared_at": budget._iso(cleared_at),
                        "cleared_by": "lead",
                    }
                ],
            }
        )
    )
    return lifts


def test_a_cleared_lift_with_no_stamp_is_backfilled_once_and_re_anchors(
    fixture: _Fixture,
) -> None:
    """A clear the stamp never reached is filled in, once, at the pace read."""
    config = _config()
    lifts = _write_cleared_lift(fixture, cleared_at=fixture.end)
    before = lifts.read_bytes()

    reading = _reading(utilisation=U_END, observed=fixture.end, reset=fixture.reset)
    bl.effective_budget(
        config,
        group=GROUP,
        readings=[reading],
        now=fixture.end + timedelta(minutes=1),
    )

    stamped = bl.list_lifts(group=GROUP)[0]
    assert stamped["u_end"] == pytest.approx(U_END)
    assert datetime.fromisoformat(stamped["t_end"]) == fixture.end
    # The figure came from the reading observed at the clear, not from one taken
    # afterwards, so it is the group's state where the lift stopped.
    assert datetime.fromisoformat(stamped["u_end_taken_at"]) == fixture.end
    assert "u_end_late" not in stamped

    after_first = lifts.read_bytes()
    assert after_first != before, "the clear was never stamped"
    bl.effective_budget(
        config,
        group=GROUP,
        readings=[reading],
        now=fixture.end + timedelta(minutes=2),
    )
    assert lifts.read_bytes() == after_first, "the clear was stamped twice"

    # And the pace reads the point it now carries: one point above the line the
    # backfilled end anchors holds, where the plain comparison alone did not.
    above = _entry(
        config, now=fixture.end, utilisation=U_END + 0.01, reset=fixture.reset
    )
    assert above["allowance"]["re_anchor"]["u_end"] == pytest.approx(U_END)
    assert budget.pace_hold(above, "implement")["held"] is True


def test_a_backfilled_figure_taken_after_the_clear_is_marked_late(
    fixture: _Fixture,
) -> None:
    """With no reading at or before the clear, the figure used is marked late."""
    config = _config()
    lifts = _write_cleared_lift(fixture, cleared_at=fixture.end)
    assert lifts.exists()

    later = fixture.end + timedelta(hours=6)
    reading = _reading(utilisation=0.62, observed=later, reset=fixture.reset)
    bl.effective_budget(
        config,
        group=GROUP,
        readings=[reading],
        now=later,
    )

    stamped = bl.list_lifts(group=GROUP)[0]
    assert stamped["u_end"] == pytest.approx(0.62)
    assert datetime.fromisoformat(stamped["t_end"]) == fixture.end
    assert datetime.fromisoformat(stamped["u_end_taken_at"]) == later
    assert stamped["u_end_late"] is True
