"""A banked budget-group reset grants one window of allowance until it is used.

A metered subscription can carry one banked reset. While it is available the
group has one extra full window of budget on top of what remains, so the pace
budget preflight and the picker read admits more for the same reading, and the
reading says ``reset_available`` so the reason is legible. Flagging it never
stacks, clearing it is explicit or detected when the window's reset boundary
jumps forward ahead of schedule. Every test isolates the crew home through the
autouse ``isolated_reckon_home`` fixture, so nothing here writes this host's
state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from reckon import budget
from reckon.cli import main as cli_main
from reckon.crew import budget_reset

NOW = datetime(2030, 1, 3, tzinfo=UTC)
PACE_MULTIPLE = 1.1
WEEK_MINUTES = 10_080
CONFIG = {
    "backends": {
        "codex": {
            "launch": "cli",
            "command": "codex",
            "budget_group": "codex-sub",
        }
    },
    "budget": {
        "pace_multiple": PACE_MULTIPLE,
        "drain_lead_hours": 12.0,
        "bookend_reserve_pct": 20.0,
    },
}


def _reading(
    *,
    resets_at: datetime,
    observed_at: datetime,
    window_minutes=WEEK_MINUTES,
    used_percent: float = 2.0,
):
    return budget._rate_limits_reading(
        {
            "primary": {
                "window_minutes": window_minutes,
                "used_percent": used_percent,
                "resets_at": int(resets_at.timestamp()),
            },
            "secondary": None,
        },
        observed_at=observed_at,
        moment=observed_at,
    )


def _allowance(reading, *, now=NOW):
    report = budget.group_pace(CONFIG, windows={"codex": reading}, now=now)
    group = next(entry for entry in report if entry["group"] == "codex-sub")
    return group["allowance"]


def test_flagging_a_banked_reset_never_stacks(isolated_reckon_home) -> None:
    first = budget_reset.mark_available("codex-sub", by="lead", moment=NOW)
    assert first["available"] is True
    assert first["changed"] is True
    assert first["set_by"] == "lead"

    second = budget_reset.mark_available("codex-sub", by="other", moment=NOW)
    assert second["available"] is True
    assert second["changed"] is False
    assert "does not stack" in second["detail"]

    # The record still carries the first setting, not the second attempt.
    found = budget_reset.record("codex-sub")
    assert found["set_by"] == "lead"
    assert found["set_at"] == first["set_at"]


def test_pace_counts_a_banked_reset_from_the_same_reading(isolated_reckon_home) -> None:
    reading = _reading(
        resets_at=NOW + timedelta(days=4), observed_at=NOW, used_percent=6.0
    )

    without = _allowance(reading)
    # No banked reset: the allowance is exactly what it was before the flag
    # existed, so a replay that recomputes it from the same row reproduces it.
    assert "reset_available" not in without

    budget_reset.mark_available("codex-sub", by="lead", moment=NOW)

    with_reset = _allowance(reading)

    assert with_reset["reset_available"] is True
    # One extra full window of allowance for the same reading: the derived
    # allowance doubles and the burn, measured against the larger budget,
    # halves.
    assert with_reset["derived"] == pytest.approx(without["derived"] * 2.0)
    assert with_reset["burn_multiple"] == pytest.approx(without["burn_multiple"] / 2.0)
    assert with_reset["effective_limit"] == pytest.approx(
        without["effective_limit"] * 2.0
    )


def test_a_consumed_reset_clears_the_flag_from_a_pair_of_readings(
    isolated_reckon_home,
) -> None:
    budget_reset.mark_available("codex-sub", by="lead", moment=NOW)
    scheduled = NOW + timedelta(days=4)

    # First reading: the boundary is four days out, still in the future.
    _allowance(_reading(resets_at=scheduled, observed_at=NOW))
    assert budget_reset.available("codex-sub") is True

    # Second reading, an hour later: the boundary jumped to about one window
    # ahead while the boundary last seen had not yet arrived. That is a reset
    # consumed ahead of schedule.
    later = NOW + timedelta(hours=6)
    _allowance(
        _reading(resets_at=later + timedelta(minutes=WEEK_MINUTES), observed_at=later)
    )

    assert budget_reset.available("codex-sub") is False
    found = budget_reset.record("codex-sub")
    consumed = [event for event in found["events"] if event["kind"] == "consumed"]
    assert len(consumed) == 1
    assert consumed[0]["previous_resets_at"].startswith("2030-01-07T00:00:00")
    assert consumed[0]["resets_at"] is not None


def test_a_natural_reset_does_not_clear_the_flag(isolated_reckon_home) -> None:
    budget_reset.mark_available("codex-sub", by="lead", moment=NOW)
    scheduled = NOW + timedelta(days=1)

    _allowance(_reading(resets_at=scheduled, observed_at=NOW))
    assert budget_reset.available("codex-sub") is True

    # The window reaches its scheduled boundary and restarts. The boundary last
    # seen is behind the observation, so this is a natural reset and the flag
    # stands.
    later = scheduled + timedelta(hours=2)
    _allowance(
        _reading(resets_at=later + timedelta(minutes=WEEK_MINUTES), observed_at=later)
    )

    assert budget_reset.available("codex-sub") is True
    found = budget_reset.record("codex-sub")
    assert not [
        event for event in found.get("events", []) if event["kind"] == "consumed"
    ]


def test_clearing_an_absent_flag_changes_nothing(isolated_reckon_home) -> None:
    result = budget_reset.mark_used("codex-sub", moment=NOW)
    assert result["available"] is False
    assert result["changed"] is False
    assert "nothing changed" in result["detail"]


def test_a_concurrent_clear_is_not_lost_to_an_in_flight_observation(
    isolated_reckon_home, monkeypatch
) -> None:
    """An observation cannot write back a flag a concurrent clear has removed.

    The store lock is held across the read and the write, so a clear that lands
    while an observation is in flight waits for the write and then stands: the
    observation never resurrects an expired flag. The contention is staged on the
    observation's own write, which is the moment the read is already decided.
    """
    import threading

    budget_reset.mark_available("codex-sub", by="lead", moment=NOW)
    started = threading.Event()
    finished = threading.Event()
    cleared: list = []

    def clear() -> None:
        started.set()
        cleared.append(budget_reset.mark_used("codex-sub", by="lead", moment=NOW))
        finished.set()

    real_write = budget_reset._write
    contender: list = []

    def write_while_clearing(data, *, home=None):
        if not contender:
            thread = threading.Thread(target=clear)
            thread.start()
            assert started.wait(timeout=5.0)
            # The clear contends for the lock this observation holds, so it
            # cannot land until the observation has written.
            assert not finished.wait(timeout=0.2)
            contender.append(thread)
        return real_write(data, home=home)

    monkeypatch.setattr(budget_reset, "_write", write_while_clearing)

    try:
        state = budget_reset.observe(
            "codex-sub",
            resets_at=NOW + timedelta(days=4),
            window_minutes=WEEK_MINUTES,
            moment=NOW,
            utilisation=2.0,
        )
    finally:
        # The observation's lock is released on return, so the waiting clear
        # proceeds now.
        if contender:
            contender[0].join(timeout=5.0)
        monkeypatch.setattr(budget_reset, "_write", real_write)

    assert state["changed"] is True
    assert finished.is_set()
    assert cleared and cleared[0]["changed"] is True
    # The clear stands; the observation did not write the clear's flag back.
    assert budget_reset.available("codex-sub") is False


def test_cli_budget_reset_round_trip(isolated_reckon_home) -> None:
    home = isolated_reckon_home
    runner = CliRunner()

    def invoke(*flags):
        return runner.invoke(
            cli_main, ["crew", "budget-reset", "--group", "codex-sub", *flags]
        )

    state = invoke()
    assert state.exit_code == 0
    first = json.loads(state.output)
    assert first["available"] is False

    flagged = invoke("--available")
    assert flagged.exit_code == 0
    payload = json.loads(flagged.output)
    assert payload["available"] is True
    assert payload["changed"] is True
    assert payload["set_by"]

    again = invoke("--available")
    again_payload = json.loads(again.output)
    assert again_payload["available"] is True
    assert again_payload["changed"] is False

    read = invoke()
    assert json.loads(read.output)["available"] is True

    used = invoke("--used")
    assert used.exit_code == 0
    assert json.loads(used.output)["available"] is False

    cleared = invoke()
    assert json.loads(cleared.output)["available"] is False

    # Every write landed under the isolated home and nowhere else.
    store = home / "crew" / budget_reset.STORE_FILENAME
    assert store.exists()
    assert store.is_relative_to(home)
