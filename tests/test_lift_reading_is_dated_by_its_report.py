"""A window figure is dated by the instant its source reported it.

The published document reconciles one account's windows by recency: whichever
candidate carries the newest observation time speaks for that window. A Codex
session's rollout receipt carries no stamp of its own, so a reader that returns
one can only age it by the run record that named the session -- and that record
is stamped no earlier than the moment the provider reported the figure. Dating
the figure by that later stamp lets an old figure outrank a newer one: a run
whose record is stamped after another rollout's report drives the published
utilisation down and, on the pace path, ends a lift that should stand.

These tests hold one run's rollout at an older figure than a second rollout's
report, and stamp the first run's record later still. The published seven-day
figure must be the newer one, dated by the instant its own rollout reported it,
and a reset-anchored lift granted at that figure must stay in force. Every home
is pointed at a temporary crew home so the live workstation's published
document and lift record are never written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_lift as bl
from reckon.crew import paid_lanes
from reckon.crew import rollout as rollout_module
from tests.real_home_traces import traces_naming

GROUP = "lift-guard-fixture"
RUN_ID = "r-live-demo"
SESSION = "sess-live"
ENV = {"USER": "lead"}

# The run's rollout reports its figure first, a second rollout reports a higher
# figure next, and the first run's record is stamped last of all. The record
# stamp is the freshest instant in the setup, so a reading aged by it would be
# read as the newest the account has -- the defect these tests pin.
NOW = datetime.now(UTC)
T0 = NOW - timedelta(minutes=3)
T1 = NOW - timedelta(minutes=2)
T2 = NOW - timedelta(minutes=1)
RESETS = NOW + timedelta(days=3)

WEEK_MINUTES = 10_080
EARLIER = 38.0
NEWER = 39.0


def _rollout(
    root: Path,
    observed: datetime,
    used_percent: float,
    *,
    session_id: str | None = None,
) -> None:
    """Write a Codex rollout whose newest rate-limit event carries one figure."""
    folder = root / observed.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{observed.strftime('%H%M%S')}.jsonl"
    lines = []
    if session_id is not None:
        lines.append(
            {
                "timestamp": observed.isoformat(),
                "type": "session_meta",
                "payload": {"session_id": session_id},
            }
        )
    lines.append(
        {
            "timestamp": observed.isoformat(),
            "type": "event_msg",
            "payload": {
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": used_percent,
                        "window_minutes": WEEK_MINUTES,
                        "resets_at": int(RESETS.timestamp()),
                    },
                }
            },
        }
    )
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _live_pointer() -> dict:
    """The live run record, stamped at the last of the three instants."""
    return {
        "run_id": RUN_ID,
        "project": "demo",
        "backend": "codex",
        "phase": "running",
        "session_id": SESSION,
        "observed_at": T2.isoformat(),
        "created_at": T2.isoformat(),
    }


def _receipt(used_percent: float) -> object:
    """The live session's rollout receipt: keyed windows, no stamp of its own."""
    return rollout_module.RolloutReceipt(
        cumulative_input_tokens=0,
        cumulative_cached_input_tokens=0,
        cumulative_output_tokens=0,
        maximum_request_input_tokens=0,
        requests_over_threshold=0,
        model_context_window=0,
        quota_readings={
            WEEK_MINUTES: rollout_module.QuotaReading(
                window_minutes=WEEK_MINUTES,
                used_percent=used_percent,
                resets_at=int(RESETS.timestamp()),
            )
        },
        plan_type="pro",
        generation_seconds=0.0,
        machine_seconds=0.0,
    )


def _week_reading(utilisation: float) -> dict:
    """A window reading in the shape the lift resolver reads."""
    return {
        "observed_at": NOW.isoformat(),
        "seven_day": {
            "utilisation": utilisation,
            "resets_at": RESETS.isoformat(),
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


_REAL_HOME = Path.home() / ".config" / "reckon"
_REAL_PUBLISHED = Path.home() / "public" / "reckon" / "paid-lanes.json"


def _real_lift_traces() -> list[str]:
    """The real lift record carrying this suite's own group, if any.

    The needle is the group this suite keys its grants by, so a leaked write
    names itself and a peer's own lift is never a hit.
    """
    return traces_naming(_REAL_HOME, (bl.LIFTS_LEAF,), (GROUP,))


def _real_published_traces() -> list[str]:
    """The real paid-lanes document carrying this run's own figure stamp.

    The needle is the run-time ``T1`` stamp the case's own published entry is
    dated by, so a scheduled refresh of the live document -- which stamps its
    own instant -- is never a hit.
    """
    return traces_naming(
        _REAL_PUBLISHED.parent, (_REAL_PUBLISHED.name,), (T1.isoformat(),)
    )


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    """Point every home at a temp crew home and prove the live ones untouched."""
    before_lifts = _real_lift_traces()
    before_published = _real_published_traces()

    home = tmp_path / "crew-home"
    monkeypatch.setenv(paid_lanes.RECKON_HOME_ENV, str(home))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield home

    after_lifts = _real_lift_traces()
    after_published = _real_published_traces()

    assert after_lifts == before_lifts, (
        f"a test wrote the live budget-lifts.json: {after_lifts}"
    )
    assert after_published == before_published, (
        f"a test wrote the live paid-lanes: {after_published}"
    )


def _gather(crew_home: Path, *, with_reader: bool) -> dict:
    """Gather codex with the run's rollout home beside the main sessions home."""
    run_sessions = crew_home / "crew" / "runs" / RUN_ID / "codex-home" / "sessions"
    main_sessions = crew_home / "codex-home" / "sessions"
    _rollout(run_sessions, T0, EARLIER, session_id=SESSION)
    _rollout(main_sessions, T1, NEWER)
    if with_reader:
        return paid_lanes.gather_sources(
            ["codex"],
            records=[],
            pointers=[_live_pointer()],
            rollouts=lambda session_id: _receipt(EARLIER),
        )
    return paid_lanes.gather_sources(["codex"], records=[], pointers=[_live_pointer()])


def test_the_newer_rollout_figure_is_published_even_though_the_record_is_later(
    crew_home,
):
    sources = _gather(crew_home, with_reader=True)
    dated = [
        candidate.reading.figure("seven_day").observed_at
        for candidate in sources["codex"]
        if candidate.reading.figure("seven_day") is not None
    ]
    assert dated, "no codex candidate carried a seven-day figure"
    assert max(dated) == T1

    entry = paid_lanes.account_entry("codex", sources["codex"], moment=NOW)
    week = entry["windows"]["seven_day"]
    assert week["utilisation"] == NEWER / 100.0
    assert week["source"] == "rollout"
    assert datetime.fromisoformat(week["observed_at"]) == T1


def test_a_lift_granted_at_the_published_figure_stays_in_force(crew_home):
    sources = _gather(crew_home, with_reader=True)
    entry = paid_lanes.account_entry("codex", sources["codex"], moment=NOW)
    week = entry["windows"]["seven_day"]
    published = _week_reading(week["utilisation"])

    config = _config()
    lift = bl.grant(
        config,
        group=GROUP,
        reason="run a build hard",
        multiple=1.8,
        readings=[published],
        now=NOW,
        environ=ENV,
    )
    assert lift["ends"]["utilisation"] == pytest.approx(NEWER / 100.0)

    assert (
        bl.end_cause(lift, readings=[published], now=NOW, bound=bl.ceilings(config))
        is None
    )

    # The older figure the record-stamped candidate would have published reads
    # as a fall below the grant and ends the lift.
    assert (
        bl.end_cause(
            lift,
            readings=[_week_reading(EARLIER / 100.0)],
            now=NOW,
            bound=bl.ceilings(config),
        )
        == bl.END_RESET
    )
