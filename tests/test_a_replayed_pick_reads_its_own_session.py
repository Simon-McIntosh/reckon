"""A replayed dispatch is judged against the budget view for its own session.

The picker composes its budget view for the session it routes for on dispatch,
on the dry run and inside ``pick()``, so a session-scoped lift releases reserves
for that session alone. The replay was left out: it composed one session-less
view and handed it to every replayed pick, so a session-scoped lift was
invisible to exactly the measurement meant to show its effect. These tests hold
the replay to composing one view per distinct session and judging each record
against its own.

The declared negative control — restoring the single session-less view in the
replay — fails the A-session case: the A dispatch then reads the group held at
the reserve ceiling instead of released.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import ledger
from reckon.crew import budget_lift
from reckon.crew.picker import replay as replay_module

NOW = datetime.now(UTC)
GROUP = "codex-sub"
BACKEND = "codex"
CEILING_WITH_RESERVES = 92
CEILING_RELEASED = 100


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary config home, with the real lift record left untouched."""

    real = Path.home() / ".config" / "reckon" / budget_lift.LIFTS_LEAF
    before = real.read_bytes() if real.exists() else None
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(budget_lift.RUN_ID_ENV, raising=False)
    yield tmp_path
    assert (real.read_bytes() if real.exists() else None) == before


def _config():
    return {
        "default_backend": BACKEND,
        "backends": {
            BACKEND: {"launch": "cli", "command": "codex", "budget_group": GROUP}
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "coordinator_reserve_pct": 3,
            "bookend_reserve_pct": 20,
            "pace_multiple": 1.1,
            "lift": {"max_multiple": 3, "max_hours": 168},
        },
        "review_excluded_backends": [],
    }


def _reading_record(run_id: str, session: str, moment: datetime) -> dict:
    """One committed run record carrying the group's window read at 95%."""

    return ledger.build_record(
        run_id=run_id,
        plan="replay-session-fixture",
        gate="failed",
        project="fixture",
        node=run_id,
        section="dispatch",
        role="implement",
        spec_level="guided",
        backend=BACKEND,
        agent={"model": "gpt-5"},
        dispatched_at=moment.isoformat(),
        completed_at=moment.isoformat(),
        session=session,
        budget={
            "headroom": "known",
            "utilisation_pct": 95,
            "resets_at": (moment + timedelta(hours=2)).isoformat(),
        },
    )


def _grant(config, session, *, now=NOW):
    budget_lift.grant(
        config,
        group=GROUP,
        reason="spend the remaining window",
        form=budget_lift.UNCAPPED,
        scope=budget_lift.SESSION_PREFIX + session,
        now=now,
        environ={},
    )


def _codex(view):
    return next(entry for entry in view["backends"] if entry["backend"] == BACKEND)


def _selection():
    return {
        "backend": BACKEND,
        "model": "gpt-5",
        "fallback_reason": None,
        "latency_ms": 1,
        "jev_latency_ms": 0,
        "usage": {},
    }


def _replay_capturing_views(monkeypatch, repo, config, records, *, count, views=None):
    """Replay ``records``, capturing the budget view each dispatch was judged on.

    ``pick`` is stubbed so the capture is of what the replay itself handed it,
    which is the decision this node owns: the dataset read, the view composition
    and the snapshotter build all run for real. ``views`` is an optional sink
    for the count of ``budget_view`` calls and the sessions each was asked for.
    """

    monkeypatch.setattr(replay_module.ledger, "runs", lambda *a, **k: records)
    if views is not None:
        real_view = replay_module.snapshot.budget_view

        def counting(*a, **k):
            views.append(k.get("session"))
            return real_view(*a, **k)

        monkeypatch.setattr(replay_module.snapshot, "budget_view", counting)
    captured: dict[str, dict] = {}

    def fake_pick(
        request,
        config,
        *,
        repo,
        records,
        snapshotter,
        verdict_inputs,
        budget_snapshot,
    ):
        captured[request.session] = budget_snapshot
        return SimpleNamespace(as_dict=_selection)

    monkeypatch.setattr(replay_module, "pick", fake_pick)
    replay_module.replay("fixture", count, config, repo=repo)
    return captured


def test_a_dispatch_is_judged_against_its_own_sessions_view(home, monkeypatch):
    """The A dispatch reads the group released; the B dispatch reads it held.

    One lift scoped to session A is in force over a group read at 95%. A replay
    of one A dispatch and one B dispatch judges each against its own session, so
    A reads the ceiling with the reserves released and B reads it still held at
    the reserve ceiling.
    """

    config = _config()
    _grant(config, "A")
    records = [
        _reading_record("newer", "A", NOW),
        _reading_record("older", "B", NOW - timedelta(minutes=5)),
    ]

    captured = _replay_capturing_views(monkeypatch, home, config, records, count=2)

    assert _codex(captured["A"])["held"] is False
    assert _codex(captured["A"])["effective_ceiling_pct"] == CEILING_RELEASED
    assert _codex(captured["B"])["held"] is True
    assert _codex(captured["B"])["effective_ceiling_pct"] == CEILING_WITH_RESERVES


def test_a_replay_of_one_session_composes_one_view(home, monkeypatch):
    """Many dispatches from one session share a single composed view."""

    config = _config()
    _grant(config, "A")
    records = [
        _reading_record(f"run-{index}", "A", NOW - timedelta(minutes=index))
        for index in range(4)
    ]

    views: list[str] = []
    _replay_capturing_views(monkeypatch, home, config, records, count=4, views=views)

    assert views == ["A"]


def test_a_record_naming_no_session_reads_the_sessionless_view(home, monkeypatch):
    """A record with no session reads the view a sessionless preflight composes."""

    config = _config()
    _grant(config, "A")
    records = [_reading_record("sessionless", "", NOW)]

    views: list[str] = []
    captured = _replay_capturing_views(
        monkeypatch, home, config, records, count=1, views=views
    )

    assert views == [""]
    # The lift is scoped to A, so the session-less view is the held one.
    assert _codex(captured[""])["held"] is True
    assert _codex(captured[""])["effective_ceiling_pct"] == CEILING_WITH_RESERVES
