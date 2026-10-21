"""Each metered lane's row says what it is spending and what it has left.

A lane's window ceiling, its whole-window burn average and its reset say
nothing about how fast the account is spending now or what it can afford over
the hours ahead, so a router weighing a node's priority against the lane's
availability has no figure for either. The burn block is that figure, derived
once in ``reckon.budget`` from the lane's own recorded readings as a series and
rendered by the picker on every metered candidate.

The figures are the ones that motivated the work: a lane that went from 4% to
20% in two hours is spending 8 points an hour whether its window is two percent
through or half spent, and a lane at 96% with six and a half days left may
afford about 0.15 points over the next six hours. Under a 10,000-credit lift
whose reserve is a tenth, a balance fallen from 60,559.94 to 59,559.94 leaves an
8,000-credit pool drawn at 2,000 an hour, which is four hours of runway.

The negative controls derive the window rate from the whole-window average
instead of the trailing span -- which the 2%-elapsed case must fail -- and leave
the reserve out of the pool, which the pool case must fail.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import budget
from reckon.crew import budget_lift as bl
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, prompts, snapshot
from tests.real_home_traces import traces_naming

PROJECT = "reckon"
GROUP = "codex-sub"
LOCAL = "local-lane"
LANE = "codex-lane"
NOW = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)
PERIOD_MINUTES = 10_080
WEEK_HOURS = PERIOD_MINUTES / 60.0
CAP = 10_000.0
BALANCE_AT_GRANT = 60_559.94

#: The namespace every run id this file mints is filed under. The needle arm of
#: the real-home guard searches for the identifiers this file alone writes, and
#: neither the project name nor a plain word like "pool" would do: a peer's live
#: pointer carries the project name in every record, and a fixture word turns up
#: in the goals of unrelated nodes, so either would report a peer's write.
FIXTURE_TAG = "burnfixture"

#: Every run id this file minted since the case began.
_MINTED: list[str] = []


def _config() -> dict[str, Any]:
    return {
        "local_backend": LOCAL,
        "default_backend": LOCAL,
        "roles": {"implement": {}},
        "routing": {"budget_period_hours": 6},
        "budget": {
            "pace_multiple": 1.1,
            "utilisation_ceiling_pct": 100.0,
            "resume_reserve_pct": 5.0,
        },
        "backends": {
            LOCAL: {"launch": "cli", "command": "clive", "model": "m-local"},
            LANE: {
                "launch": "cli",
                "command": "codex",
                "model": "m-codex",
                "budget_group": GROUP,
            },
        },
    }


def _live_pointer_entries(home: Path) -> set[str]:
    """The names filed in a crew home's live-pointer directory."""

    try:
        return {entry.name for entry in (home / "crew" / "live").iterdir()}
    except OSError:
        return set()


def _named_run_id(pointer: Path, attempts: int = 4) -> str | None:
    """The run id a live pointer names, or nothing where it names none.

    A crew pointer is written whole and records the run it belongs to. A file
    that reads here as unparseable is either not a pointer at all or is being
    written at this instant, so it is read a few times before it is judged: a
    peer's dispatch writes into this directory continuously, and a torn read of
    one must not read as a trace of this case.
    """

    for attempt in range(attempts):
        try:
            record = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if attempt + 1 < attempts:
                time.sleep(0.05)
                continue
            return None
        run_id = record.get("run_id") if isinstance(record, Mapping) else None
        return run_id if isinstance(run_id, str) and run_id else None
    return None


def _writes_into_the_live_home(
    home: Path, before: set[str], minted: tuple[str, ...]
) -> list[str]:
    """What a case wrote into a shared crew home, or nothing.

    Two arms, and neither alone is enough. The needle search looks for the
    identifiers this case mints, which is exact but passes vacuously over a
    case that mints none. The listing arm reads the live-pointer directory
    against an earlier reading, so it catches a write whatever the write
    named. Being a shared directory, it needs one thing more: a pointer that
    appeared while the case ran and names a run this case did not mint belongs
    to a peer's dispatch, which is the ordinary traffic of that directory
    rather than a trace of this case. What no peer produces is a file there
    naming no run at all, since a pointer is written by the code that owns the
    run it describes; that is what the listing arm reports.
    """

    appeared = sorted(_live_pointer_entries(home) - before)
    live = home / "crew" / "live"
    return [
        *(
            f"a file naming no run of its own appeared in the live-pointer directory: {name}"
            for name in appeared
            if _named_run_id(live / name) is None
        ),
        *traces_naming(home, ("crew/live",), minted),
    ]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the crew home, and assert the real one is left untouched."""

    home = Path.home() / ".config" / "reckon"
    _MINTED.clear()
    before = _live_pointer_entries(home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield
    leaked = _writes_into_the_live_home(home, before, tuple(_MINTED))
    assert not leaked, f"a test wrote the live crew home: {leaked}"


def test_the_home_guard_reports_a_file_that_appears_beneath_it(tmp_path: Path):
    """The guard fails on a file appearing in the live-pointer directory.

    The real crew home is shared with every peer session, so the control paints
    nothing into it: it stands a temporary home up and drives the same helpers
    the guard runs, which is the path that has to fail. Three readings, so
    neither a helper that always reported a leak nor one that reported nothing
    could pass: a clean directory reports nothing, a file naming no run at all
    is reported, and a peer's own well-formed pointer is not.
    """

    home = tmp_path / "shared-home"
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    before = _live_pointer_entries(home)
    assert _writes_into_the_live_home(home, before, ()) == []

    (live / "burnfixture-leak.json").write_text("{}", encoding="utf-8")
    leaked = _writes_into_the_live_home(home, before, ())
    assert leaked, "a file appearing in the live-pointer directory was not reported"
    assert "burnfixture-leak.json" in leaked[0]

    (live / "burnfixture-leak.json").unlink()
    (live / "a-peers-run.json").write_text(
        json.dumps({"run_id": "a-run-this-case-never-minted", "project": PROJECT}),
        encoding="utf-8",
    )
    assert _writes_into_the_live_home(home, before, ()) == [], (
        "a peer's own live pointer was read as this case's write"
    )


def test_the_home_needle_arm_reports_a_pointer_filed_under_a_minted_run_id(
    tmp_path: Path,
):
    """A well-formed pointer carrying a minted id is still this case's write.

    The listing arm leaves a pointer that names a run alone, because a peer's
    dispatch writes exactly that shape. The needle arm closes the gap: a
    pointer filed under an identifier this case minted is reported however
    well-formed it is.
    """

    home = tmp_path / "shared-home"
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    before = _live_pointer_entries(home)
    minted = f"{FIXTURE_TAG}-leaked"
    (live / f"{minted}.json").write_text(
        json.dumps({"run_id": minted, "project": PROJECT}), encoding="utf-8"
    )

    leaked = _writes_into_the_live_home(home, before, (minted,))
    assert leaked, "a pointer filed under a minted run id was not reported"
    assert minted in leaked[0]


def _record(
    home: Path,
    *,
    run_id: str,
    when: datetime,
    utilisation_pct: float,
    hours_to_reset: float,
    balance: float | None = None,
    backend: str = LANE,
) -> None:
    """One recorded budget block, as a live run's pointer carries it."""

    block: dict[str, Any] = {
        "headroom": "known",
        "utilisation_pct": utilisation_pct,
        "rate_limit_type": "primary",
        "rate_limit_period_minutes": PERIOD_MINUTES,
        "resets_at": (when + timedelta(hours=hours_to_reset)).isoformat(),
        "threshold_status": "ok",
    }
    if balance is not None:
        block["credits"] = {
            "has_credits": True,
            "unlimited": False,
            "balance": balance,
        }
    minted = f"{FIXTURE_TAG}-{run_id}"
    pointer = {
        "run_id": minted,
        "project": PROJECT,
        "backend": backend,
        "observed_at": when.isoformat(),
        "budget": block,
    }
    _MINTED.append(minted)
    path = home / "crew" / "live" / f"{minted}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pointer), encoding="utf-8")


def _lift_block() -> dict[str, Any]:
    """The credit lift as the effective budget block carries it."""

    return {
        "id": "lift-credit-fixture",
        "cap": CAP,
        "balance_at_grant": BALANCE_AT_GRANT,
        "reserve_fraction": 0.1,
    }


def _publish(
    home: Path, *, when: datetime, utilisation: float, resets_in: float
) -> None:
    """The headroom document one lane member publishes, under the crew home."""

    _publish_windows(home, when=when, windows={"seven_day": (utilisation, resets_in)})


def _publish_windows(
    home: Path,
    *,
    when: datetime,
    windows: Mapping[str, tuple[float, float]],
) -> None:
    """The same document carrying one observed block per named window.

    Each value is the window's utilisation as a fraction and the hours to its
    reset. Blocks keep the order given, so a case can put the window it does
    not mean first and require the reader to name the one it does.
    """

    document = {
        "version": 1,
        "accounts": {
            LANE: {
                "windows": {
                    period: {
                        "state": "observed",
                        "utilisation": utilisation,
                        "observed_at": when.isoformat(),
                        "resets_at": (when + timedelta(hours=resets_in)).isoformat(),
                    }
                    for period, (utilisation, resets_in) in windows.items()
                }
            }
        },
    }
    (home / "paid-lanes.json").write_text(json.dumps(document), encoding="utf-8")


def _burn(config: dict | None = None) -> dict[str, Any]:
    return budget.burn_block(
        PROJECT, GROUP, config=_config() if config is None else config, now=NOW
    )


def _grant_ending(config: dict, hours: float) -> dict:
    return bl.grant(
        config,
        group=GROUP,
        reason="spend purchased credits on high-level work",
        form=bl.CREDITS,
        credits=CAP,
        balance_at_grant=BALANCE_AT_GRANT,
        ends={"kind": "at", "at": (NOW + timedelta(hours=hours)).isoformat()},
        now=NOW,
        environ={"USER": "lead"},
    )


# --- the window's own rate ---------------------------------------------------


def test_a_young_window_still_reports_what_it_is_spending_now(tmp_path: Path):
    """4% then 20% two hours later is 8 points an hour at 2% elapsed."""

    hours_to_reset = WEEK_HOURS * 0.98
    _record(
        tmp_path,
        run_id="young-a",
        when=NOW - timedelta(hours=2),
        utilisation_pct=4.0,
        hours_to_reset=hours_to_reset + 2,
    )
    _record(
        tmp_path,
        run_id="young-b",
        when=NOW,
        utilisation_pct=20.0,
        hours_to_reset=hours_to_reset,
    )

    burn = _burn()
    assert burn["window_rate_pct_per_hour"] == pytest.approx(8.0, rel=0.001)
    # The density the floor withholds: at 2% elapsed the whole-window average
    # reads null, where the trailing rate does not.
    assert (
        budget._burn_multiple(20.0, PERIOD_MINUTES, int(hours_to_reset * 3600)) is None
    )


def test_the_window_even_share_is_the_remainder_over_the_hours_to_reset(
    tmp_path: Path,
):
    """96% with 6.5 days left affords about 0.15 points over six hours."""

    _record(
        tmp_path,
        run_id="even-share",
        when=NOW,
        utilisation_pct=96.0,
        hours_to_reset=6.5 * 24,
    )

    burn = _burn()
    assert burn["window_even_share_pct"] == pytest.approx(0.15, abs=0.01)
    assert burn["window_rate_pct_per_hour"] is None


def test_one_window_carries_both_the_rate_and_the_even_share(tmp_path: Path):
    """A document publishing both windows is read on the window the lane ran on.

    The recorded reading places the seven-day window, so the published document
    must be read there too. It lists the five-hour window first, where a reader
    taking the first figure it finds would report 43 points an hour against 12
    points of even share instead of the seven-day window's 8 and 3.08.
    """

    _record(
        tmp_path,
        run_id="both-a",
        when=NOW - timedelta(hours=2),
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24 + 2,
    )
    _publish_windows(
        tmp_path,
        when=NOW,
        windows={"five_hour": (0.90, 5.0), "seven_day": (0.20, 6.5 * 24)},
    )

    burn = _burn()
    assert burn["window_rate_pct_per_hour"] == pytest.approx(8.0, rel=0.001)
    assert burn["window_even_share_pct"] == pytest.approx(3.08, abs=0.01)


def test_the_published_headroom_document_supplies_the_current_point(
    tmp_path: Path,
):
    """A recorded 4% two hours ago and a published 20% now is 8 points an hour."""

    _record(
        tmp_path,
        run_id="published-a",
        when=NOW - timedelta(hours=2),
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24 + 2,
    )
    _publish(tmp_path, when=NOW, utilisation=0.20, resets_in=6.5 * 24)

    burn = _burn()
    assert burn["window_rate_pct_per_hour"] == pytest.approx(8.0, rel=0.001)
    # The published point's own remainder, 80 points over 156 hours, quoted for
    # the six-hour period.
    assert burn["window_even_share_pct"] == pytest.approx(3.08, abs=0.01)


# --- the credits lift --------------------------------------------------------


def test_the_pool_is_the_cap_less_its_reserve_less_the_credits_drawn(
    tmp_path: Path,
):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="pool",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["credit_pool_remaining"] == pytest.approx(8_000.0)
    assert burn["credit_pool_cap"] == pytest.approx(10_000.0)


def test_the_credit_rate_and_runway_follow_the_balance_fall(tmp_path: Path):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="credit-a",
        when=NOW - timedelta(hours=1),
        utilisation_pct=3.0,
        hours_to_reset=6.5 * 24 + 1,
        balance=BALANCE_AT_GRANT + 1000.0,
    )
    _record(
        tmp_path,
        run_id="credit-b",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["credit_rate_per_hour"] == pytest.approx(2_000.0, rel=0.001)
    assert burn["runway_hours"] == pytest.approx(4.0, rel=0.001)


def test_the_credit_even_share_is_the_pool_over_the_hours_to_the_end(
    tmp_path: Path,
):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="pool-even",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["credit_even_share"] == pytest.approx(1_000.0, rel=0.001)


def test_the_credit_fields_are_absent_with_no_credits_lift(tmp_path: Path):
    _record(
        tmp_path,
        run_id="no-lift",
        when=NOW,
        utilisation_pct=96.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn()
    assert "credit_pool_remaining" not in burn
    assert "credit_pool_cap" not in burn
    assert "credit_rate_per_hour" not in burn
    assert "credit_even_share" not in burn
    assert burn["window_even_share_pct"] == pytest.approx(0.15, abs=0.01)


def test_one_reading_rates_nothing(tmp_path: Path):
    config = _config()
    _grant_ending(config, 48.0)
    _record(
        tmp_path,
        run_id="single",
        when=NOW,
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["window_rate_pct_per_hour"] is None
    assert burn["credit_rate_per_hour"] is None
    assert burn["runway_hours"] is None


def test_two_readings_under_the_minimum_span_are_one_sample(tmp_path: Path):
    config = _config()
    config["budget"] = {**config["budget"], "credit_lift": _lift_block()}
    _record(
        tmp_path,
        run_id="close-a",
        when=NOW - timedelta(minutes=10),
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24 + 0.2,
        balance=BALANCE_AT_GRANT,
    )
    _record(
        tmp_path,
        run_id="close-b",
        when=NOW,
        utilisation_pct=20.0,
        hours_to_reset=6.5 * 24,
        balance=BALANCE_AT_GRANT - 1000.0,
    )

    burn = _burn(config)
    assert burn["window_rate_pct_per_hour"] is None
    assert burn["credit_rate_per_hour"] is None
    assert burn["credit_pool_remaining"] == pytest.approx(8_000.0)


# --- the picker renders it ---------------------------------------------------


def _request() -> PickRequest:
    return PickRequest(
        project=PROJECT,
        node=TaskNode(
            id="burn-node",
            goal="see each lane's burn",
            plan="lane-and-model-routing",
            role="implement",
            spec_level="exact",
            done_when="each metered lane's row carries its burn block",
            write_paths=[],
        ),
        session="s",
        capability={},
        estimated_context=8000,
    )


def _view(observed_at: str) -> dict[str, Any]:
    return {
        "groups": [
            {
                "members": [LANE],
                "allowance": {
                    "state": budget.OBSERVED,
                    "utilisation": 0.20,
                    "burn_multiple": 1.0,
                    "effective_limit": None,
                    "resets_at": (NOW + timedelta(hours=6.5 * 24)).isoformat(),
                    "reset_available": False,
                    "observed_at": observed_at,
                },
            }
        ],
        "backends": [
            {"backend": LOCAL, "held": False, "state": {"headroom": "unknown"}},
            {
                "backend": LANE,
                "held": False,
                "state": {
                    "headroom": "known",
                    "utilisation_pct": 20.0,
                    "burn_multiple": 1.0,
                    "resets_at": (NOW + timedelta(hours=6.5 * 24)).isoformat(),
                    "expired": False,
                    "observed_at": observed_at,
                    "source": "account-surface",
                },
            },
        ],
    }


@pytest.fixture
def _no_fleet_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze every fleet read so a pick touches only the code under test."""

    monkeypatch.setattr(snapshot, "_fit", lambda *a, **k: [])
    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *a, **k: 0)
    monkeypatch.setattr(
        snapshot,
        "_serving_observation",
        lambda backend: {"status": "serving", "detail": ""},
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )


@pytest.mark.usefixtures("_no_fleet_reads")
def test_a_picks_rendered_state_carries_the_burn_block_on_metered_candidates(
    tmp_path: Path,
):
    # A pick reads the wall clock rather than a moment handed to it, so this
    # case dates its readings from now; the pure budget cases above state the
    # moment they are judged at instead.
    live = datetime.now(UTC)
    _record(
        tmp_path,
        run_id="rendered-a",
        when=live - timedelta(hours=2),
        utilisation_pct=4.0,
        hours_to_reset=6.5 * 24 + 2,
    )
    _record(
        tmp_path,
        run_id="rendered-b",
        when=live,
        utilisation_pct=20.0,
        hours_to_reset=6.5 * 24,
    )
    request = _request()
    options = snapshot.candidates(
        request,
        _config(),
        Path("/nonexistent-repo"),
        records=[],
        verdict_inputs={},
        budget_snapshot=_view(NOW.isoformat()),
        cached_only=True,
    )
    state = prompts.build_state(
        node=request.node,
        capability={},
        estimated_context=8000,
        comment="",
        candidates=options,
        lane={"return_times": {}, "lanes": {}, "local_lane": {}},
    )
    rows = state["candidates"]
    metered = next(row for row in rows.values() if row["backend"] == LANE)
    local = next(row for row in rows.values() if row["backend"] == LOCAL)
    assert metered["burn"]["window_rate_pct_per_hour"] == pytest.approx(8.0, rel=0.001)
    assert "burn" not in local
