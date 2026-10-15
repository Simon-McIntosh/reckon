"""Budget preflight and picker judge the same effective group block as routing."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget, ledger
from reckon.crew import budget_group, budget_lift
from reckon.crew.picker import snapshot

NOW = datetime(2026, 10, 15, 7, tzinfo=UTC)
GROUP = "codex-sub"
BACKEND = "codex"


@pytest.fixture
def home(tmp_path, monkeypatch):
    real = Path.home() / ".config" / "reckon" / budget_lift.LIFTS_LEAF
    before = real.read_bytes() if real.exists() else None
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(budget_lift.RUN_ID_ENV, raising=False)
    yield tmp_path
    assert (real.read_bytes() if real.exists() else None) == before


def _config(*, excluded=()):
    return {
        "backends": {
            BACKEND: {"launch": "cli", "command": "codex", "budget_group": GROUP}
        },
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "coordinator_reserve_pct": 3,
            "bookend_reserve_pct": 20,
            "pace_multiple": 1.1,
            "lift": {"max_multiple": 3, "max_hours": 168},
        },
        "review_excluded_backends": list(excluded),
    }


def _record():
    return ledger.build_record(
        run_id="reading",
        plan="budget-fixture",
        gate="failed",
        agent={"backend": BACKEND},
        completed_at=NOW.isoformat(),
        budget={
            "headroom": "known",
            "utilisation_pct": 95,
            "resets_at": (NOW + timedelta(hours=2)).isoformat(),
        },
    )


def _preflight(config, home, *, session=None):
    return budget.preflight(
        "fixture", config, root=home, records=[_record()], now=NOW, session=session
    )["backends"][0]


def _grant(config, *, session=None):
    return budget_lift.grant(
        config,
        group=GROUP,
        reason="spend the remaining window",
        form=budget_lift.UNCAPPED,
        scope=budget_lift.GLOBAL
        if session is None
        else budget_lift.SESSION_PREFIX + session,
        now=NOW,
        environ={},
    )


def test_global_lift_releases_preflight_and_picker_and_clear_restores_hold(home):
    config = _config()
    assert _preflight(config, home)["effective_ceiling_pct"] == 92
    _grant(config)
    verdict = _preflight(config, home)
    assert verdict["effective_ceiling_pct"] == 100
    assert verdict["held"] is False

    view = snapshot.budget_view(
        "fixture", config, home, [_record()], now=NOW, cache_root=home / "cache"
    )
    assert view["backends"][0]["effective_ceiling_pct"] == 100
    assert view["backends"][0]["held"] is False

    budget_lift.clear(config, group=GROUP, now=NOW, environ={})
    verdict = _preflight(config, home)
    assert verdict["effective_ceiling_pct"] == 92
    assert verdict["held"] is True


def test_session_lift_releases_only_its_named_session(home):
    config = _config()
    _grant(config, session="mine")
    assert _preflight(config, home, session="mine")["held"] is False
    assert _preflight(config, home, session="other")["effective_ceiling_pct"] == 92
    assert _preflight(config, home)["held"] is True


def test_lift_granted_after_cached_view_is_read_on_next_view(home):
    config = _config()
    cache = home / "cache"
    before = snapshot.budget_view(
        "fixture", config, home, [_record()], now=NOW, cache_root=cache
    )
    assert before["backends"][0]["held"] is True
    _grant(config)
    after = snapshot.budget_view(
        "fixture", config, home, [_record()], now=NOW, cache_root=cache
    )
    assert after["backends"][0]["effective_ceiling_pct"] == 100
    assert after["backends"][0]["held"] is False
    budget_lift.clear(config, group=GROUP, now=NOW, environ={})
    cleared = snapshot.budget_view(
        "fixture", config, home, [_record()], now=NOW, cache_root=cache
    )
    assert cleared["backends"][0]["effective_ceiling_pct"] == 92
    assert cleared["backends"][0]["held"] is True


def test_review_exclusion_uses_the_routing_block(home, monkeypatch):
    config = _config(excluded=(BACKEND,))
    routing_block = budget_group.effective_block(config, GROUP, now=NOW)
    seen = []
    effective_block = budget_group.effective_block

    def recording_block(*args, **kwargs):
        block = effective_block(*args, **kwargs)
        seen.append(block)
        return block

    monkeypatch.setattr(budget_group, "effective_block", recording_block)
    assert _preflight(config, home)["held"] is True
    assert (
        seen
        and seen[0]["bookend_reserve_pct"] == routing_block["bookend_reserve_pct"] == 0
    )
