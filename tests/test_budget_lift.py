"""The lift record and its resolver: what a lift is, and when it stops.

Every assertion here is derived from the fixtures at assertion time rather than
written down as a literal, so a fixture date moving does not silently change
what a test means. The negative control this file declares — replacing
:func:`effective_budget` with one that returns ``config["budget"]`` unchanged —
is applied on a scratch copy, not here; these tests are the green arm.

Each test points the config home at a temporary directory and asserts the real
``budget-lifts.json`` is untouched afterwards, because a test that writes the
live workstations's lift record is a monitor for the machine rather than a test
of the code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from reckon import _store
from reckon._flight_schema import BudgetConfig
from reckon.crew import budget_lift as bl
from reckon.crew import pace as pace_module
from reckon.crew import reserve as reserve_module

WEEK_HOURS = bl.CLOCK_HOURS[bl.SEVEN_DAY]
GROUP = "codex-sub"
NOW = datetime(2026, 10, 8, 3, 12, tzinfo=UTC)
ENV = {"USER": "lead"}


def _reset(*, elapsed_hours: float) -> datetime:
    """The seven-day reset a group shows after ``elapsed_hours`` of its week."""
    return NOW + timedelta(hours=WEEK_HOURS - elapsed_hours)


def _reading(
    *,
    week_utilisation: float,
    elapsed_hours: float,
    five_utilisation: float = 0.05,
    observed_at: datetime | None = None,
    week_resets_at: datetime | None = None,
) -> dict:
    return {
        "observed_at": (observed_at or NOW).isoformat(),
        "five_hour": {
            "utilisation": five_utilisation,
            "resets_at": (observed_at or NOW + timedelta(hours=4)).isoformat(),
        },
        "seven_day": {
            "utilisation": week_utilisation,
            "resets_at": (
                week_resets_at or _reset(elapsed_hours=elapsed_hours)
            ).isoformat(),
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


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the config home at a temp dir; prove the real record is untouched."""
    real = Path.home() / ".config" / "reckon" / bl.LIFTS_LEAF
    before = real.read_bytes() if real.exists() else None
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after = real.read_bytes() if real.exists() else None
    assert after == before, "a test wrote the live budget-lifts.json"


def test_record_lives_under_the_config_home_at_the_declared_leaf(tmp_path):
    assert bl.lifts_path() == _store._config_home() / bl.LIFTS_LEAF
    assert bl.lifts_path() == tmp_path / bl.LIFTS_LEAF


def test_each_write_raises_the_version_and_uses_the_atomic_writer(
    tmp_path, monkeypatch
):
    calls: list[Path] = []
    original = _store.write_json_atomically

    def spy(path, payload, **kwargs):
        calls.append(Path(path))
        return original(path, payload, **kwargs)

    monkeypatch.setattr(_store, "write_json_atomically", spy)
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config, group=GROUP, reason="first", multiple=1.8, readings=[reading], now=NOW
    )
    bl.grant(
        config, group=GROUP, reason="second", multiple=2.0, readings=[reading], now=NOW
    )
    assert calls == [tmp_path / bl.LIFTS_LEAF, tmp_path / bl.LIFTS_LEAF]
    document = bl.read_document()
    assert document["version"] == 2
    assert len(document["lifts"]) == 2


def test_a_lift_carries_every_recorded_field():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    lift = bl.grant(
        config,
        group=GROUP,
        reason="spend the week",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        environ=ENV,
    )
    for field in (
        "id",
        "group",
        "pace_multiple",
        "ends",
        "granted_by",
        "granted_at",
        "reason",
        "form",
        "scope",
        "starts_at",
        "cleared_by",
        "cleared_at",
    ):
        assert field in lift, field
    assert lift["group"] == GROUP
    assert lift["reason"] == "spend the week"
    assert lift["granted_by"] == "lead"
    assert lift["form"] == bl.MULTIPLE
    assert lift["scope"] == bl.GLOBAL
    assert lift["pace_multiple"] == pytest.approx(1.8)
    assert lift["granted_at"] == NOW.isoformat()
    assert lift["starts_at"] == NOW.isoformat()
    assert lift["cleared_by"] is None and lift["cleared_at"] is None


def test_grant_refuses_an_undeclared_group():
    config = _config()
    with pytest.raises(bl.LiftRefusedError):
        bl.grant(config, group="never-declared", reason="why", multiple=1.8, now=NOW)


def test_grant_refuses_a_multiple_at_or_below_the_configured_one():
    config = _config()
    for offered in (1.1, 0.5):
        with pytest.raises(bl.LiftRefusedError):
            bl.grant(config, group=GROUP, reason="why", multiple=offered, now=NOW)


def test_grant_refuses_a_multiple_above_the_declared_ceiling():
    config = _config()
    with pytest.raises(bl.LiftRefusedError):
        bl.grant(
            config,
            group=GROUP,
            reason="why",
            multiple=bl.DEFAULT_MAX_MULTIPLE + 0.1,
            now=NOW,
        )


def test_grant_refuses_a_missing_reason():
    config = _config()
    for reason in (None, "", "   "):
        with pytest.raises(bl.LiftRefusedError):
            bl.grant(config, group=GROUP, reason=reason, multiple=1.8, now=NOW)


def test_grant_refuses_inside_a_fenced_run():
    config = _config()
    with pytest.raises(bl.LiftRefusedError):
        bl.grant(
            config,
            group=GROUP,
            reason="why",
            multiple=1.8,
            now=NOW,
            environ={bl.RUN_ID_ENV: "r-20261008T132610418827-x"},
        )


def test_uncapped_grant_is_exempt_from_the_multiple_bounds():
    config = _config()
    lift = bl.grant(
        config, group=GROUP, reason="drain it", form=bl.UNCAPPED, now=NOW, environ=ENV
    )
    assert lift["form"] == bl.UNCAPPED
    assert lift["pace_multiple"] is None


def test_a_grant_records_its_projected_exhaustion_and_admits_it():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    lift = bl.grant(
        config,
        group=GROUP,
        reason="spend",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        environ=ENV,
    )
    assert lift["projected_exhaustion"] is not None
    assert bl._parse_stamp(lift["projected_exhaustion"]) < _reset(elapsed_hours=22.0)


def test_effective_budget_returns_the_lifted_block_with_reserves_released():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    lift = bl.grant(
        config,
        group=GROUP,
        reason="spend",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        environ=ENV,
    )
    block = bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
    assert block is not config["budget"]
    assert block["pace_multiple"] == pytest.approx(1.8)
    assert block["lift_id"] == lift["id"]
    assert block["resume_reserve_pct"] == 0.0
    assert block["coordinator_reserve_pct"] == 0.0
    assert block["bookend_reserve_pct"] == 0.0
    # The lifted block admits the burn the configured multiple would hold, and
    # opens the implementation ceiling to the whole window.
    elapsed_fraction = 22.0 / WEEK_HOURS
    burn = 0.21 / elapsed_fraction
    assert burn > config["budget"]["pace_multiple"]
    assert burn <= block["pace_multiple"]
    assert reserve_module.role_ceiling_pct(block, "implement") == pytest.approx(100.0)


def test_effective_budget_is_unchanged_for_a_moved_reset():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config, group=GROUP, reason="spend", multiple=1.8, readings=[reading], now=NOW
    )
    moved = _reading(
        week_utilisation=0.22,
        elapsed_hours=22.0,
        week_resets_at=_reset(elapsed_hours=22.0) + timedelta(hours=1),
    )
    assert (
        bl.effective_budget(config, group=GROUP, readings=[moved], now=NOW)
        is config["budget"]
    )


def test_effective_budget_is_unchanged_past_its_stated_time():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config,
        group=GROUP,
        reason="spend",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        ends={"kind": "at", "at": (NOW - timedelta(hours=1)).isoformat()},
    )
    assert (
        bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
        is config["budget"]
    )


def test_effective_budget_is_unchanged_for_a_cleared_lift():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config, group=GROUP, reason="spend", multiple=1.8, readings=[reading], now=NOW
    )
    assert bl.clear(config, group=GROUP, now=NOW) is not None
    assert (
        bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
        is config["budget"]
    )


def test_max_hours_bounds_a_lift_whose_reset_never_moves():
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    base = NOW - timedelta(hours=30)
    tight = _config(lift={"max_hours": 24.0})
    loose = _config()
    bl.grant(
        tight, group=GROUP, reason="spend", multiple=1.8, readings=[reading], now=base
    )
    assert (
        bl.effective_budget(tight, group=GROUP, readings=[reading], now=NOW)
        is tight["budget"]
    )
    assert (
        bl.effective_budget(loose, group=GROUP, readings=[reading], now=NOW)
        is not loose["budget"]
    )


def test_effective_budget_is_unchanged_when_utilisation_falls_below_the_grant_figure():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config, group=GROUP, reason="spend", multiple=1.8, readings=[reading], now=NOW
    )
    below = _reading(week_utilisation=0.10, elapsed_hours=22.0)
    assert (
        bl.effective_budget(config, group=GROUP, readings=[below], now=NOW)
        is config["budget"]
    )


def test_effective_budget_is_unchanged_for_a_fall_between_two_readings():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config, group=GROUP, reason="spend", multiple=1.8, readings=[reading], now=NOW
    )
    history = [
        _reading(week_utilisation=0.30, elapsed_hours=22.0),
        _reading(week_utilisation=0.25, elapsed_hours=23.0),
    ]
    assert (
        bl.effective_budget(config, group=GROUP, readings=history, now=NOW)
        is config["budget"]
    )


def test_effective_budget_is_unchanged_before_a_future_start():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config,
        group=GROUP,
        reason="spend",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        starts_at=NOW + timedelta(hours=1),
    )
    assert (
        bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
        is config["budget"]
    )


def test_drain_by_holds_above_the_line_and_admits_below_it():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    lift = bl.grant(
        config,
        group=GROUP,
        reason="land at friday",
        form=bl.DRAIN_BY,
        target=NOW + timedelta(hours=24),
        readings=[reading],
        now=NOW,
        environ=ENV,
    )
    halfway = NOW + timedelta(hours=12)
    line = bl.drain_line(lift, now=halfway)
    assert line > lift["u0"]
    assert bl.under_pace_hold(lift, utilisation=line + 0.05, now=halfway) is True
    assert bl.under_pace_hold(lift, utilisation=line - 0.05, now=halfway) is False


def test_a_drain_by_target_past_the_reset_is_clamped_to_it():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    lift = bl.grant(
        config,
        group=GROUP,
        reason="later",
        form=bl.DRAIN_BY,
        target=_reset(elapsed_hours=22.0) + timedelta(hours=48),
        readings=[reading],
        now=NOW,
        environ=ENV,
    )
    assert lift["target"] == _reset(elapsed_hours=22.0).isoformat()


def test_an_uncapped_lift_marks_no_pace_hold():
    config = _config()
    reading = _reading(week_utilisation=0.99, elapsed_hours=22.0)
    lift = bl.grant(
        config, group=GROUP, reason="spend it", form=bl.UNCAPPED, now=NOW, environ=ENV
    )
    block = bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
    assert block["pace_hold"] == {"kind": bl.UNCAPPED, "held": False}
    assert bl.under_pace_hold(lift, utilisation=0.99, now=NOW) is False


def test_a_session_lift_governs_only_its_own_session():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    global_lift = bl.grant(
        config, group=GROUP, reason="all", multiple=1.8, readings=[reading], now=NOW
    )
    session_lift = bl.grant(
        config,
        group=GROUP,
        reason="mine",
        multiple=2.0,
        readings=[reading],
        now=NOW,
        scope="session",
        session="s1",
    )
    mine = bl.effective_budget(
        config, group=GROUP, readings=[reading], now=NOW, session="s1"
    )
    theirs = bl.effective_budget(
        config, group=GROUP, readings=[reading], now=NOW, session="s2"
    )
    assert mine["lift_id"] == session_lift["id"]
    assert mine["pace_multiple"] == pytest.approx(2.0)
    assert theirs["lift_id"] == global_lift["id"]
    assert theirs["pace_multiple"] == pytest.approx(1.8)


def test_a_session_lift_alone_does_not_cover_another_session():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config,
        group=GROUP,
        reason="mine",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        scope="session",
        session="s1",
    )
    assert (
        bl.effective_budget(
            config, group=GROUP, readings=[reading], now=NOW, session="s2"
        )
        is config["budget"]
    )
    assert (
        bl.effective_budget(
            config, group=GROUP, readings=[reading], now=NOW, session="s1"
        )
        is not config["budget"]
    )


# ── clear names the lift it revokes ─────────────────────────────────────────


def _global_and_session_lifts(config, reading):
    """Grant a global lift and a session lift for ``s1``, returning both."""
    global_lift = bl.grant(
        config, group=GROUP, reason="all", multiple=1.5, readings=[reading], now=NOW
    )
    session_lift = bl.grant(
        config,
        group=GROUP,
        reason="mine",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        scope="session",
        session="s1",
    )
    return global_lift, session_lift


def test_clear_with_a_session_revokes_that_session_lift_leaving_the_global():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    global_lift, session_lift = _global_and_session_lifts(config, reading)
    assert (
        bl.effective_budget(
            config, group=GROUP, readings=[reading], now=NOW, session="s1"
        )["lift_id"]
        == session_lift["id"]
    )

    cleared = bl.clear(config, group=GROUP, session="s1", now=NOW)
    assert cleared is not None and cleared["id"] == session_lift["id"]

    after = bl.effective_budget(
        config, group=GROUP, readings=[reading], now=NOW, session="s1"
    )
    assert after["lift_id"] == global_lift["id"]
    assert after["pace_multiple"] == pytest.approx(global_lift["pace_multiple"])


def test_clear_by_id_revokes_a_session_lift():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    global_lift, session_lift = _global_and_session_lifts(config, reading)

    cleared = bl.clear(config, group=GROUP, lift_id=session_lift["id"], now=NOW)
    assert cleared is not None and cleared["id"] == session_lift["id"]

    after = bl.effective_budget(
        config, group=GROUP, readings=[reading], now=NOW, session="s1"
    )
    assert after["lift_id"] == global_lift["id"]


def test_clear_without_a_session_or_id_revokes_only_a_global_lift():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    global_lift, session_lift = _global_and_session_lifts(config, reading)

    cleared = bl.clear(config, group=GROUP, now=NOW)
    assert cleared is not None and cleared["id"] == global_lift["id"]

    mine = bl.effective_budget(
        config, group=GROUP, readings=[reading], now=NOW, session="s1"
    )
    assert mine["lift_id"] == session_lift["id"]
    assert (
        bl.effective_budget(
            config, group=GROUP, readings=[reading], now=NOW, session="s2"
        )
        is config["budget"]
    )


def test_clear_returns_none_when_only_a_session_lift_exists():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config,
        group=GROUP,
        reason="mine",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        scope="session",
        session="s1",
    )
    assert bl.clear(config, group=GROUP, now=NOW) is None


# ── the returned block never carries a None pace multiple ───────────────────


def test_effective_budget_keeps_the_configured_multiple_for_a_drain_by_lift():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config,
        group=GROUP,
        reason="land at friday",
        form=bl.DRAIN_BY,
        target=NOW + timedelta(hours=24),
        readings=[reading],
        now=NOW,
    )
    block = bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
    assert block is not config["budget"]
    assert block["pace_multiple"] == pytest.approx(config["budget"]["pace_multiple"])
    assert block["pace_hold"]["kind"] == bl.DRAIN_BY


def test_effective_budget_keeps_the_configured_multiple_for_an_uncapped_lift():
    config = _config()
    reading = _reading(week_utilisation=0.99, elapsed_hours=22.0)
    bl.grant(
        config,
        group=GROUP,
        reason="spend it",
        form=bl.UNCAPPED,
        readings=[reading],
        now=NOW,
    )
    block = bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
    assert block["pace_multiple"] == pytest.approx(config["budget"]["pace_multiple"])
    assert block["pace_hold"] == {"kind": bl.UNCAPPED, "held": False}


def test_effective_budget_never_returns_a_none_pace_multiple():
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    for form in (bl.MULTIPLE, bl.DRAIN_BY, bl.UNCAPPED):
        grant = {"multiple": 1.8} if form == bl.MULTIPLE else {}
        if form == bl.DRAIN_BY:
            grant["target"] = NOW + timedelta(hours=24)
        bl.grant(
            config,
            group=GROUP,
            reason="spend",
            form=form,
            readings=[reading],
            now=NOW,
            **grant,
        )
        block = bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
        assert block["pace_multiple"] is not None, form
        bl.clear(config, group=GROUP, now=NOW)


# ── the configured multiple is read from the pace policy ────────────────────


def test_configured_pace_multiple_reads_the_pace_policy():
    config = _config()
    assert bl.configured_pace_multiple(config) == pytest.approx(
        pace_module.policy(config).pace_multiple
    )
    retuned = _config(pace_multiple=1.4)
    assert bl.configured_pace_multiple(retuned) == pytest.approx(1.4)
    assert bl.configured_pace_multiple({}) == pytest.approx(
        pace_module.policy({}).pace_multiple
    )


def test_effective_budget_ignores_a_session_environment_variable(monkeypatch):
    """The session is the argument; a resolver reading the environment would
    answer a different question from the one its caller asked."""
    config = _config()
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bl.grant(
        config,
        group=GROUP,
        reason="mine",
        multiple=1.8,
        readings=[reading],
        now=NOW,
        scope="session",
        session="s1",
    )
    monkeypatch.setenv("RECKON_SESSION", "s1")
    assert (
        bl.effective_budget(config, group=GROUP, readings=[reading], now=NOW)
        is config["budget"]
    )
    assert (
        bl.effective_budget(
            config, group=GROUP, readings=[reading], now=NOW, session="s1"
        )
        is not config["budget"]
    )


# ── the lift ceilings are declared flight keys ──────────────────────────────


def test_a_flight_layer_may_set_the_lift_ceilings():
    """The declared slots validate, and an undeclared key under the block does
    not, so a layer writing the ceilings is held to the same shape as any
    other."""
    accepted = BudgetConfig.model_validate(
        {"lift": {"max_multiple": 2.0, "max_hours": 24.0}}
    )
    assert accepted.lift is not None
    assert accepted.lift.max_multiple == pytest.approx(2.0)
    assert accepted.lift.max_hours == pytest.approx(24.0)
    with pytest.raises(ValidationError):
        BudgetConfig.model_validate({"lift": {"max_multiple": 2.0, "nope": 1}})


def _resolved_layer(tmp_path, lift_block: str) -> dict:
    """Resolve a host layer declaring ``lift_block`` under budget.lift."""
    from reckon import flight

    host = tmp_path / "host-flight.yaml"
    host.write_text(
        "backends:\n"
        f"  {GROUP}:\n"
        f"    budget_group: {GROUP}\n"
        "budget:\n"
        "  lift:\n"
        f"{lift_block}"
    )
    resolved = flight.resolve(host_path=host, project_path=tmp_path / "absent.yaml")
    return resolved.config


def test_a_layer_ceiling_refuses_a_lift_above_it_and_a_higher_one_admits(tmp_path):
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    bounded = _resolved_layer(tmp_path, "    max_multiple: 2\n")
    assert bounded["budget"]["lift"]["max_multiple"] == pytest.approx(2.0)
    with pytest.raises(bl.LiftRefusedError):
        bl.grant(
            bounded,
            group=GROUP,
            reason="too far",
            multiple=2.5,
            readings=[reading],
            now=NOW,
        )

    unbounded = _resolved_layer(tmp_path, "    max_multiple: 3\n")
    admitted = bl.grant(
        unbounded,
        group=GROUP,
        reason="within",
        multiple=2.5,
        readings=[reading],
        now=NOW,
    )
    assert admitted["pace_multiple"] == pytest.approx(2.5)


def test_a_layer_hour_ceiling_makes_an_old_lift_inert_and_a_loose_one_keeps_it(
    tmp_path,
):
    reading = _reading(week_utilisation=0.21, elapsed_hours=22.0)
    granted_at = NOW - timedelta(hours=30)
    tight = _resolved_layer(tmp_path, "    max_hours: 24\n")
    bl.grant(
        tight,
        group=GROUP,
        reason="spend",
        multiple=1.8,
        readings=[reading],
        now=granted_at,
    )
    assert (
        bl.effective_budget(tight, group=GROUP, readings=[reading], now=NOW)
        is tight["budget"]
    )

    loose = _resolved_layer(tmp_path, "    max_hours: 168\n")
    bl.grant(
        loose,
        group=GROUP,
        reason="spend",
        multiple=1.8,
        readings=[reading],
        now=granted_at,
    )
    assert (
        bl.effective_budget(loose, group=GROUP, readings=[reading], now=NOW)
        is not loose["budget"]
    )
