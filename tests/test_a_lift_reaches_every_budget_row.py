"""Every budget row a lift reaches takes the dispatching session and its clocks.

A lift is granted either globally or to one session, and a session lift decides
which dispatch may spend past pace without changing whose wallet the pace comes
from. That makes the dispatching session a field of every row a dispatch
composes: the pace row records it beside the lift id and the multiple in force,
the bookend refusal reads it back off the pace row it is already handed, and the
budget verdict a resume or a lane change reaches is judged against the clocks the
run was paced by. Another session's pre-flight still names a session lift on the
shared group without applying it.

Every figure is derived from the fixture at assertion time: the plan's own
measurement, 21% of a 168 h window spent after 22.0 h against a 1.1x configured
multiple. The cases drive the modules a reader reaches rather than restating
their figures.

Each case points the configuration home at a temporary directory and asserts the
workstation's own ``budget-lifts.json`` is untouched afterwards, because a case
that writes the plan's lift document reports on the machine and not on the code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget
from reckon.crew import budget_lift as bl
from reckon.crew import pace_replay
from reckon.crew import reserve as reserve_module
from reckon.crew import window_reading
from reckon.crew.dispatch_admission import _refuse_against_the_bookend_reserve
from reckon.crew.dispatch_sessions import _pace_readings
from reckon.crew.node import CrewError
from reckon.crew.routing import _budget_verdict

GROUP = "codex-sub"
LANE = "codex-sub"
PROJECT = "lift-bench"
NOW = datetime(2026, 10, 8, 3, 12, tzinfo=UTC)
ENV = {"USER": "lead"}
WEEK_HOURS = 168.0
WEEK_MINUTES = 10080
MULTIPLE = 1.1
UTILISATION = 0.21
FIVE_UTILISATION = 0.05
ELAPSED_HOURS = 22.0
LIFT_MULTIPLE = 1.8
BOOKEND_UTILISATION = 0.85
BOOKEND = 25.0
SESSION = "s1"
OTHER_SESSION = "s2"


def _week_reset(elapsed_hours=ELAPSED_HOURS):
    return NOW + timedelta(hours=WEEK_HOURS - elapsed_hours)


def _config(**overrides):
    block = {
        "pace_multiple": MULTIPLE,
        "lift": {"max_multiple": 3.0, "max_hours": 168.0},
        "utilisation_ceiling_pct": 100.0,
        "resume_reserve_pct": 5.0,
        "coordinator_reserve_pct": 3.0,
        reserve_module.RESERVE_KEY: BOOKEND,
    }
    block.update(overrides)
    return {
        "backends": {LANE: {"launch": "cli", "command": "codex", "budget_group": GROUP}},
        "budget": block,
    }


def _window(week_utilisation=UTILISATION, week_resets_at=None):
    reset = _week_reset()
    if week_resets_at is not None:
        reset = week_resets_at
    five_reset = NOW + timedelta(hours=1)
    figures = (
        window_reading.WindowFigure(
            period="five_hour",
            utilisation=FIVE_UTILISATION,
            observed_at=NOW,
            age_seconds=5.0,
            resets_at=budget._iso(five_reset),
            window_minutes=300,
        ),
        window_reading.WindowFigure(
            period="seven_day",
            utilisation=week_utilisation,
            observed_at=NOW,
            age_seconds=5.0,
            resets_at=budget._iso(reset),
            window_minutes=WEEK_MINUTES,
        ),
    )
    return window_reading.WindowReading(
        figures=figures, observed_at=NOW, age_seconds=5.0
    )


def _reading(week_utilisation=UTILISATION, week_resets_at=None):
    reset = _week_reset()
    if week_resets_at is not None:
        reset = week_resets_at
    five_reset = NOW + timedelta(hours=1)
    return {
        "observed_at": NOW.isoformat(),
        "five_hour": {
            "utilisation": FIVE_UTILISATION,
            "resets_at": five_reset.isoformat(),
        },
        "seven_day": {
            "utilisation": week_utilisation,
            "resets_at": (
                week_resets_at.isoformat()
                if week_resets_at is not None
                else reset.isoformat()
            ),
        },
    }


def _clocks(reading):
    return {
        "five_hour": {
            "period": "five_hour",
            "state": "observed",
            "utilisation": reading["five_hour"]["utilisation"],
            "resets_at": reading["five_hour"]["resets_at"],
        },
        "seven_day": {
            "period": "seven_day",
            "state": "observed",
            "utilisation": reading["seven_day"]["utilisation"],
            "resets_at": reading["seven_day"]["resets_at"],
        },
    }


def _pace_record(reading, session):
    return {
        "group": GROUP,
        "lane": LANE,
        "recorded_at": NOW.isoformat(),
        "session": session,
        "clocks": _clocks(reading),
    }


def _grant(config, **kwargs):
    kwargs.setdefault("readings", [_reading()])
    kwargs.setdefault("multiple", LIFT_MULTIPLE)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("environ", {})
    kwargs.setdefault("granted_by", "a-lift-reaches-every-budget-row")
    return bl.grant(
        config, group=GROUP, reason="spend ahead into the window", **kwargs
    )


def _pace_row(config, monkeypatch, session, window=None, node="n"):
    reading = window
    if reading is None:
        reading = _window()
    monkeypatch.setattr(budget, "recorded_windows", lambda *a, **k: {LANE: reading})
    monkeypatch.setattr(budget, "_account_surface_readings", lambda *a, **k: {})
    return budget.pace_row(
        config,
        project=PROJECT,
        lane=LANE,
        node=node,
        score=0.0,
        now=NOW,
        session=session,
    )


def _verdict_ceiling(config, home, session, readings):
    verdict = _budget_verdict(
        project=PROJECT,
        root=home / "ledger",
        config=config,
        backend_name=LANE,
        backend={"budget_group": GROUP},
        purpose="dispatch",
        budget_state={"backend": LANE, "headroom": "unknown"},
        now=NOW,
        session=session,
        readings=readings,
    )
    return float(verdict["effective_ceiling_pct"])


_REAL_LIFTS = Path.home() / ".config" / "reckon" / bl.LIFTS_LEAF


def _real_lifts_bytes():
    if _REAL_LIFTS.exists():
        return _REAL_LIFTS.read_bytes()
    return None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    before = _real_lifts_bytes()
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    assert _real_lifts_bytes() == before, "the real budget-lifts.json was written"


def test_a_pace_row_with_no_lift_records_its_session_and_no_lift(monkeypatch):
    row = _pace_row(_config(), monkeypatch, session=SESSION)

    assert row["session"] == SESSION
    assert row["lift_id"] is None
    assert row["policy"]["pace_multiple"] == pytest.approx(MULTIPLE)
    assert row["allowance"].get("lift") is None


def test_a_session_lift_marks_its_own_row_and_not_another(monkeypatch):
    config = _config()
    granted = _grant(config, scope=bl.SESSION_PREFIX + SESSION)

    mine = _pace_row(config, monkeypatch, session=SESSION)
    theirs = _pace_row(config, monkeypatch, session=OTHER_SESSION)

    assert mine["lift_id"] == granted["id"]
    assert mine["allowance"]["lift"]["id"] == granted["id"]
    assert mine["policy"]["pace_multiple"] == pytest.approx(LIFT_MULTIPLE)

    assert theirs["lift_id"] is None
    assert theirs["allowance"].get("lift") is None
    assert theirs["policy"]["pace_multiple"] == pytest.approx(MULTIPLE)


def test_the_bookend_refusal_takes_the_session_from_the_pace_row():
    config = _config()
    reading = _reading(week_utilisation=BOOKEND_UTILISATION)
    _grant(config, readings=[reading], scope=bl.SESSION_PREFIX + SESSION)

    _refuse_against_the_bookend_reserve(
        config=config,
        role="implement",
        pace_record=_pace_record(reading, SESSION),
    )

    with pytest.raises(CrewError):
        _refuse_against_the_bookend_reserve(
            config=config,
            role="implement",
            pace_record=_pace_record(reading, OTHER_SESSION),
        )


def test_the_pace_readings_helper_hands_the_row_clocks_as_a_reading(monkeypatch):
    row = _pace_row(_config(), monkeypatch, session=SESSION)

    assert _pace_readings({"pace": row}) == [row["clocks"]]
    assert _pace_readings({"pace": {"clocks": {}}}) is None
    assert _pace_readings({}) is None


def test_a_moved_seven_day_reset_ends_a_session_lift_on_routings_path(tmp_path, monkeypatch):
    config = _config()
    reading = _reading()
    _grant(config, readings=[reading], scope=bl.SESSION_PREFIX + SESSION)

    ceiling = float(config["budget"]["utilisation_ceiling_pct"])
    configured = (
        ceiling
        - float(config["budget"]["resume_reserve_pct"])
        - float(config["budget"]["coordinator_reserve_pct"])
    )

    lifted = _verdict_ceiling(config, tmp_path, SESSION, [_clocks(reading)])
    assert lifted == pytest.approx(ceiling)

    moved = _reading(week_resets_at=_week_reset() + timedelta(hours=1))
    ended = _verdict_ceiling(config, tmp_path, SESSION, [_clocks(moved)])
    assert ended == pytest.approx(configured)

    foreign = _verdict_ceiling(config, tmp_path, OTHER_SESSION, [_clocks(reading)])
    assert foreign == pytest.approx(configured)


def test_another_sessions_preflight_names_the_lift_without_applying_it():
    config = _config()
    granted = _grant(config, scope=bl.SESSION_PREFIX + SESSION)

    mine = next(
        g
        for g in budget.preflight(
            PROJECT,
            config,
            backends=[LANE],
            windows={LANE: _window()},
            now=NOW,
            session=SESSION,
        )["groups"]
        if g["group"] == GROUP
    )
    theirs = next(
        g
        for g in budget.preflight(
            PROJECT,
            config,
            backends=[LANE],
            windows={LANE: _window()},
            now=NOW,
            session=OTHER_SESSION,
        )["groups"]
        if g["group"] == GROUP
    )

    lift_block = mine["allowance"]["lift"]
    assert lift_block["id"] == granted["id"]
    assert lift_block["pace_multiple"] == pytest.approx(LIFT_MULTIPLE)
    assert mine.get("session_lift") is None

    assert theirs["allowance"].get("lift") is None
    assert theirs["allowance"]["pace_multiple"] == pytest.approx(MULTIPLE)
    assert theirs["session_lift"]["id"] == granted["id"]
    assert theirs["session_lift"]["scope"] == bl.SESSION_PREFIX + SESSION


def test_replay_reads_the_multiple_each_row_recorded(monkeypatch):
    config = _config()
    _grant(config, scope=bl.SESSION_PREFIX + SESSION)
    row = _pace_row(config, monkeypatch, session=SESSION, node="lifted")

    assert row["lift_id"] is not None
    assert row["policy"]["pace_multiple"] == pytest.approx(LIFT_MULTIPLE)

    report = pace_replay.replay([{"run_id": "run-lifted", "pace": row}])
    check = report["rows"][0]

    assert check["allowance_unmeasured"] is False

    assert check["recomputed_allowance"]["pace_multiple"] == pytest.approx(LIFT_MULTIPLE)