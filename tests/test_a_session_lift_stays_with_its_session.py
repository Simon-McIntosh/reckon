"""A session-scoped lift is drawn only on its own session's pane.

Every project's producer sweeps the workstation's whole lift document into its
own watch stream, so a row that names no owner reaches every follower in every
project, including a lift one session granted for itself. These tests grant a lift in a
temporary config home, sweep it into a temporary stream, and read the row back
through the follower's own selection rule.

The declared negative control — ``_lift_row`` writing ``"session": ""`` again —
fails the scoped-lift cases: an unattributed row is selected by every follower.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_lift as bl
from reckon.crew import lift_watch, runs
from reckon.crew_follow_commands import _follow_selects

GROUP = "codex-sub"
PROJECT = "reckon"
NOW = datetime(2026, 10, 13, 6, 53, tzinfo=UTC)
OWNER = "b8e6ba21-owner"
PEER = "0ba4e093-peer"


def _reading() -> dict:
    return {
        "observed_at": NOW.isoformat(),
        "seven_day": {
            "utilisation": 0.06,
            "resets_at": (NOW + timedelta(days=7)).isoformat(),
        },
    }


def _config() -> dict:
    return {
        "backends": {GROUP: {"budget_group": GROUP}},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
            "coordinator_reserve_pct": 3.0,
        },
    }


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


def _granted_row(tmp_path: Path, *, scope: str, session: str | None) -> dict:
    """Grant one uncapped lift, sweep it once, and return the row it wrote."""
    config = _config()
    bl.grant(
        config,
        group=GROUP,
        reason="run the build hard on the codex lane",
        form=bl.UNCAPPED,
        scope=scope,
        session=session,
        readings=[_reading()],
        granted_by="lead",
        now=NOW,
        environ={"USER": "lead"},
    )
    stream = tmp_path / "proj.events"
    lift_watch.sweep(
        PROJECT,
        seen={},
        now=NOW,
        config=config,
        readings_for=lambda group, cfg, now: [_reading()],
        stream_path=stream,
    )
    rows = [json.loads(line) for line in stream.read_text().splitlines() if line]
    assert len(rows) == 1, rows
    assert rows[0]["to_state"] == lift_watch.LIFT_GRANTED
    return rows[0]


def test_a_session_scoped_lift_row_names_its_session(tmp_path):
    row = _granted_row(tmp_path, scope="session", session=OWNER)

    assert row["session"] == OWNER


def test_another_sessions_follower_does_not_draw_a_session_scoped_lift(tmp_path):
    row = _granted_row(tmp_path, scope="session", session=OWNER)

    assert not _follow_selects(row, session=PEER)
    assert _follow_selects(row, session=OWNER)
    # A fleet-wide reader, one naming no session, still sees every lift.
    assert _follow_selects(row, session=None)


def test_a_global_lift_reaches_every_follower(tmp_path):
    row = _granted_row(tmp_path, scope=bl.GLOBAL, session=None)

    assert row["session"] == ""
    assert _follow_selects(row, session=PEER)
    assert _follow_selects(row, session=OWNER)
