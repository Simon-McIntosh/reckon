"""The picker's budget preflight is asked about the session it routes for.

A session-scoped lift releases one session's reserves and no other's, so the
picker's budget view has to be composed for the session its caller routes for.
These tests hold each composition site — the dispatch and dry-run builder, the
picker's own pick — to that, and hold the view cache to keeping two sessions'
figures apart.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import budget_lift, dispatch_picker
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, pick
from reckon.crew.picker.snapshot import budget_view

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


def _record(moment=NOW):
    return ledger.build_record(
        run_id="reading",
        plan="picker-session-fixture",
        gate="failed",
        agent={"backend": BACKEND},
        completed_at=moment.isoformat(),
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


def _node():
    return TaskNode(
        id="picker-session",
        goal="route a node for the session that asked",
        plan="example",
        section="dispatch",
        role="implement",
        spec_level="guided",
        done_when="the picker reads the session's budget",
    )


def _codex(view):
    return next(entry for entry in view["backends"] if entry["backend"] == BACKEND)


def test_the_dry_run_budget_read_names_the_session_it_was_asked_about(home):
    """The builder's budget snapshot releases the lift for A and not for B.

    The dry-run and dispatch paths read their budget view through this snapshot,
    so a session-scoped lift reaching the picker at all is decision-visible
    here: A's figure is composed for A, and B's figure for B is built in the same
    call shape before A's cache has expired.
    """

    config = _config()
    _grant(config, "A", now=NOW)
    reading = _record()

    for_a = _codex(
        dispatch_picker._picker_budget_snapshot("fixture", config, home, [reading], "A")
    )
    assert for_a["held"] is False
    assert for_a["effective_ceiling_pct"] == CEILING_RELEASED

    for_b = _codex(
        dispatch_picker._picker_budget_snapshot("fixture", config, home, [reading], "B")
    )
    assert for_b["held"] is True
    assert for_b["effective_ceiling_pct"] == CEILING_WITH_RESERVES


def test_the_builder_hands_its_session_to_the_budget_snapshot(home, monkeypatch):
    """`build_picker_inputs` passes the session it was given down the chain."""

    seen = []
    real = dispatch_picker._picker_budget_snapshot

    def spy(project, config, repo_root, records, session=""):
        seen.append(session)
        return real(project, config, repo_root, records, session)

    monkeypatch.setattr(dispatch_picker, "_picker_budget_snapshot", spy)
    dispatch_picker.build_picker_inputs(
        "fixture", _config(), home, ledger_root=home, session="A"
    )
    assert seen == ["A"]


def test_pick_hands_its_requests_session_to_the_budget_view(home):
    """`pick()` composes its view for the session the request carries."""

    config = _config()
    _grant(config, "A", now=NOW)
    captured = {}

    def snapshotter(request, config, repo, **kwargs):
        captured["view"] = kwargs["budget_snapshot"]
        return []

    pick(
        PickRequest("fixture", _node(), session="A"),
        config,
        repo=home,
        snapshotter=snapshotter,
        records=[_record()],
    )
    assert captured["view"] is not None
    assert _codex(captured["view"])["held"] is False
    assert _codex(captured["view"])["effective_ceiling_pct"] == CEILING_RELEASED


def test_a_view_composed_for_one_session_is_not_served_to_another(home):
    """The view's cache key names the session, so B never reads A's release."""

    config = _config()
    _grant(config, "A")
    cache = home / "cache"
    reading = [_record()]

    for_a = budget_view(
        "fixture", config, home, reading, now=NOW, cache_root=cache, session="A"
    )
    assert _codex(for_a)["effective_ceiling_pct"] == CEILING_RELEASED

    for_b = budget_view(
        "fixture", config, home, reading, now=NOW, cache_root=cache, session="B"
    )
    assert _codex(for_b)["held"] is True
    assert _codex(for_b)["effective_ceiling_pct"] == CEILING_WITH_RESERVES


def test_a_caller_naming_no_session_reads_the_sessionless_figures(home):
    """With no session the view is composed as a sessionless preflight leaves it."""

    config = _config()
    _grant(config, "A")
    view = budget_view(
        "fixture", config, home, [_record()], now=NOW, cache_root=home / "cache"
    )
    assert _codex(view)["held"] is True
    assert _codex(view)["effective_ceiling_pct"] == CEILING_WITH_RESERVES
