"""A limit reset is one durable event in the banked-reset store, not a message.

When the account's usage limit resets early, every session that stopped at the
limit stays idle until someone tells it to continue. The announcement writes one
event into the group's record in ``budget-resets.json``, and that event is what a
pane draws, a sweep announces and a listing shows. Several sessions relaying the
same news must produce one event rather than several, a session-scoped event
reaches one session and leaves the banked flag alone, and ``recent_resets`` is
the single reader all of them go through.

Every test isolates the crew home through the autouse ``isolated_reckon_home``
fixture, so nothing here writes this host's state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import budget_reset

NOW = datetime(2030, 1, 3, 12, 0, tzinfo=UTC)
WEEK_MINUTES = 10_080
GROUP = "codex-sub"


def _stamp(moment: datetime) -> str:
    """The boundary form the window readings carry."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _events(group: str = GROUP) -> list[dict]:
    found = budget_reset.record(group) or {}
    return list(found.get("events") or [])


def _budget(*flags):
    return CliRunner().invoke(cli_main, ["crew", "budget", *flags])


def test_used_writes_one_event_with_every_field_on_a_group_with_no_flag(
    isolated_reckon_home,
) -> None:
    result = budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW,
        reason="the limit reset early",
        clock="five_hour",
    )

    assert result["recorded"] is True
    events = _events()
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "used"
    assert event["id"] == result["id"]
    assert event["id"].startswith(f"reset-{GROUP}-")
    assert event["detected_at"] == "2030-01-03T12:00:00Z"
    assert event["clock"] == "five_hour"
    assert event["by"] == "lead"
    assert event["reason"] == "the limit reset early"
    # A global event names no session.
    assert "session" not in event
    # Nothing was being held, so no flag moved and the event stands on its own.
    assert budget_reset.available(GROUP) is False
    assert result["event"]["group"] == GROUP


def test_a_global_used_clears_the_flag_and_a_scoped_one_leaves_it(
    isolated_reckon_home,
) -> None:
    budget_reset.mark_available(GROUP, by="lead", moment=NOW)
    scoped = budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW + timedelta(minutes=1),
        reason="rehearsal for one session",
        clock="seven_day",
        session="s-rehearsal",
    )

    assert scoped["recorded"] is True
    assert budget_reset.available(GROUP) is True
    assert [event["session"] for event in _events()] == ["s-rehearsal"]

    cleared = budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW + timedelta(minutes=2),
        reason="the account's limit reset",
        clock="seven_day",
    )

    assert cleared["recorded"] is True
    assert cleared["changed"] is True
    assert budget_reset.available(GROUP) is False
    # Both events are on record: a rehearsal does not stand in for the reset.
    assert len(_events()) == 2
    assert "session" not in _events()[-1]


def test_used_without_a_reason_is_refused_and_writes_nothing(
    isolated_reckon_home,
) -> None:
    result = _budget("--reset", "--group", GROUP, "--used")

    assert result.exit_code != 0
    assert "--reason" in result.output
    assert budget_reset.record(GROUP) is None
    assert not budget_reset.state_path().exists()


def test_from_and_session_are_recorded_as_given(isolated_reckon_home) -> None:
    result = _budget(
        "--reset",
        "--group",
        GROUP,
        "--used",
        "--reason",
        "the lead relayed the reset",
        "--session",
        "s-one",
        "--from",
        "2026-10-21T14:26:00Z",
        "--clock",
        "five_hour",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["recorded"] is True
    events = _events()
    assert len(events) == 1
    assert events[0]["detected_at"] == "2026-10-21T14:26:00Z"
    assert events[0]["session"] == "s-one"
    assert events[0]["clock"] == "five_hour"
    assert events[0]["reason"] == "the lead relayed the reset"
    # A session-scoped event is a rehearsal and does not touch the flag.
    assert budget_reset.available(GROUP) is False


def test_a_repeat_within_ten_minutes_is_one_event_and_later_is_another(
    isolated_reckon_home,
) -> None:
    first = budget_reset.mark_used(
        GROUP, by="lead-a", moment=NOW, reason="reported by lead-a", clock="seven_day"
    )
    again = budget_reset.mark_used(
        GROUP,
        by="lead-b",
        moment=NOW + timedelta(minutes=5),
        reason="reported by lead-b",
        clock="seven_day",
    )

    assert again["recorded"] is False
    assert again["id"] == first["id"]
    assert again["event"]["detected_at"] == first["event"]["detected_at"]
    assert len(_events()) == 1
    assert _events()[0]["by"] == "lead-a"

    later = budget_reset.mark_used(
        GROUP,
        by="lead-c",
        moment=NOW + timedelta(minutes=30),
        reason="a second reset",
        clock="seven_day",
    )

    assert later["recorded"] is True
    assert later["id"] != first["id"]
    assert len(_events()) == 2


def test_a_repeat_on_another_clock_is_another_event(isolated_reckon_home) -> None:
    first = budget_reset.mark_used(
        GROUP, by="lead", moment=NOW, reason="seven day", clock="seven_day"
    )
    other = budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW + timedelta(minutes=1),
        reason="five hour",
        clock="five_hour",
    )

    assert other["recorded"] is True
    assert other["id"] != first["id"]
    assert len(_events()) == 2


def test_recent_resets_returns_what_applies_to_one_session(
    isolated_reckon_home,
) -> None:
    now = NOW + timedelta(hours=7)
    horizon = timedelta(hours=8)

    budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW - timedelta(hours=3),
        reason="older than the horizon",
        clock="five_hour",
    )
    budget_reset.mark_used(
        GROUP, by="lead", moment=NOW + timedelta(minutes=30), reason="global"
    )
    budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW + timedelta(minutes=40),
        reason="mine",
        session="s-mine",
    )
    budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=NOW + timedelta(minutes=50),
        reason="theirs",
        session="s-other",
    )

    # A consumed reset, detected from a pair of window readings, is the same kind
    # of news and comes back through the same reader.
    later = NOW + timedelta(hours=6)
    budget_reset.mark_available(GROUP, by="lead", moment=NOW)
    budget_reset.observe(
        GROUP,
        resets_at=_stamp(NOW + timedelta(days=4)),
        window_minutes=WEEK_MINUTES,
        moment=NOW,
        utilisation=2.0,
    )
    budget_reset.observe(
        GROUP,
        resets_at=_stamp(later + timedelta(minutes=WEEK_MINUTES)),
        window_minutes=WEEK_MINUTES,
        moment=later,
        utilisation=2.0,
    )

    mine = budget_reset.recent_resets("s-mine", now=now, horizon=horizon)
    reasons = [row.get("reason") for row in mine]

    assert "global" in reasons
    assert "mine" in reasons
    assert "theirs" not in reasons
    assert "older than the horizon" not in reasons
    assert any(row["kind"] == "consumed" for row in mine)
    assert all(row["group"] == GROUP for row in mine)
    assert all(row["id"].startswith(f"reset-{GROUP}-") for row in mine)

    # Naming no session sees every event, which is what a sweep announcing the
    # session-scoped ones needs.
    every = budget_reset.recent_resets(now=now, horizon=horizon)
    assert {"global", "mine", "theirs"} <= {row.get("reason") for row in every}


def test_a_consumed_event_carries_an_id_in_the_same_form(
    isolated_reckon_home,
) -> None:
    later = NOW + timedelta(hours=6)
    budget_reset.mark_available(GROUP, by="lead", moment=NOW)
    budget_reset.observe(
        GROUP,
        resets_at=_stamp(NOW + timedelta(days=4)),
        window_minutes=WEEK_MINUTES,
        moment=NOW,
        utilisation=2.0,
    )
    budget_reset.observe(
        GROUP,
        resets_at=_stamp(later + timedelta(minutes=WEEK_MINUTES)),
        window_minutes=WEEK_MINUTES,
        moment=later,
        utilisation=2.0,
    )

    consumed = [event for event in _events() if event["kind"] == "consumed"]
    assert len(consumed) == 1
    assert consumed[0]["id"].startswith(f"reset-{GROUP}-")


def test_list_shows_the_last_days_events_and_omits_older_ones(
    isolated_reckon_home,
) -> None:
    wall = datetime.now(UTC)
    budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=wall - timedelta(hours=30),
        reason="yesterday's reset",
        clock="five_hour",
    )
    budget_reset.mark_used(
        GROUP,
        by="lead",
        moment=wall - timedelta(hours=1),
        reason="an hour ago",
        clock="five_hour",
    )

    result = _budget("--list", "--group", GROUP)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    reasons = [row["reason"] for row in payload["resets"]]

    assert "an hour ago" in reasons
    assert "yesterday's reset" not in reasons
    assert payload["reset_count"] == len(payload["resets"])


def test_the_cli_round_trips_a_used_reset(isolated_reckon_home) -> None:
    flagged = _budget("--reset", "--group", GROUP, "--available")
    assert flagged.exit_code == 0, flagged.output
    assert json.loads(flagged.output)["available"] is True

    used = _budget(
        "--reset",
        "--group",
        GROUP,
        "--used",
        "--reason",
        "the limit reset",
        "--by",
        "lead",
    )
    assert used.exit_code == 0, used.output
    payload = json.loads(used.output)
    assert payload["available"] is False
    assert payload["changed"] is True
    assert payload["recorded"] is True
    assert payload["event"]["reason"] == "the limit reset"

    state = _budget("--reset", "--group", GROUP)
    assert json.loads(state.output)["available"] is False

    # Every write landed under the isolated home and nowhere else.
    home = isolated_reckon_home
    store = home / "crew" / budget_reset.STORE_FILENAME
    assert store.exists()
    assert store.is_relative_to(home)
