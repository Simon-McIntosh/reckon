"""A probe reading from a shared account surface is never shown as a lane's own.

The account probe issues an account-level call, so every backend declaring the
account command is probed identically whatever its subscription.  Identical
readings therefore prove nothing about a shared budget, and the group that owns
a probe reading is declared in configuration rather than inferred from any
observable the surface holds.  These tests pin the lanes view's half of that
rule: a lane outside the probed account's declared group renders no figure at
all rather than the probed account's number, and the suppression is not blanket
- a lane inside that group, and a lane holding a reading of its own, each keep
carrying their own measurement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from reckon import _backends, mcp_views
from reckon.crew.rollout import Unmeasured

WEEKLY_MINUTES = 7 * 24 * 60
FIVE_HOUR_MINUTES = 5 * 60
WEEKLY_RESET = WEEKLY_MINUTES * 10**5
FIVE_HOUR_RESET = WEEKLY_RESET - WEEKLY_MINUTES
CONTEXT_WINDOW = WEEKLY_MINUTES * 12 + FIVE_HOUR_MINUTES * 2
OBSERVED_AT = "2030-01-02T03:04:05Z"
COMPOSED_AT = "2030-01-02T03:05:06Z"

# The shared account probe's figure: the subscription the probe reads, which is
# not the one a lane outside its declared group draws on.
PROBED_USED_PERCENT = 85.0


@dataclass(frozen=True)
class _Reading:
    window_minutes: int
    used_percent: Any
    resets_at: Any


@dataclass(frozen=True)
class _Receipt:
    model_context_window: Any
    quota_readings: Any


def _account_probe_block() -> dict[str, Any]:
    """The account probe's answer, in the shape the on-disk copy carries.

    A cached account block states its figure as a scalar percentage beside the
    period it belongs to rather than as a quota-window mapping, so this is the
    shape that reaches the lane rows as one synthesised window.
    """

    return {
        _backends.ACCOUNT_CACHE_STAMP: OBSERVED_AT,
        "fetch_age_seconds": 30.0,
        "headroom": "known",
        "utilisation_pct": PROBED_USED_PERCENT,
        "rate_limit_period_minutes": WEEKLY_MINUTES,
        "resets_at": _backends._epoch_to_iso(WEEKLY_RESET),
        "detail": "account quota probe answered",
    }


def _compose(
    backends: Mapping[str, Mapping[str, Any]],
    receipts: Mapping[str, _Receipt],
    sessions: Mapping[str, str],
) -> dict[str, Any]:
    runs = [
        {
            "run_id": f"run-{backend}",
            "backend": backend,
            "session_id": session,
            "completed_at": OBSERVED_AT,
        }
        for backend, session in sessions.items()
    ]
    return mcp_views.crew_lanes_view(
        {"backends": dict(backends)},
        runs,
        receipt_reader=receipts.__getitem__,
        probe_reader=lambda _backend, _settings: _account_probe_block(),
        composed_at=COMPOSED_AT,
    )


def _lanes_by_backend(view: dict[str, Any]) -> dict[str, Any]:
    return {row["backend"]: row for row in view["lanes"]}


def _divergent_backends() -> dict[str, dict[str, Any]]:
    """Two codex backends on one subscription, plus one on its own."""

    return {
        "codex": {
            "launch": "cli",
            "command": "codex",
            "budget_group": "codex-sub",
        },
        "codex-luna": {
            "launch": "cli",
            "command": "codex",
            "budget_group": "codex-sub",
        },
        "codex-spark": {
            "launch": "cli",
            "command": "codex",
            "budget_group": "spark-sub",
        },
    }


def _sol_receipt(used_percent: Any) -> _Receipt:
    return _Receipt(
        CONTEXT_WINDOW,
        {WEEKLY_MINUTES: _Reading(WEEKLY_MINUTES, used_percent, WEEKLY_RESET)},
    )


def test_a_lane_outside_the_probed_group_carries_no_borrowed_figure() -> None:
    """The spark shape: token usage, no headroom, another account's probe."""

    receipts = {
        "sol-session": _sol_receipt(40),
        "spark-session": _Receipt(CONTEXT_WINDOW, Unmeasured.NO_RATE_LIMITS),
    }
    sessions = {"codex": "sol-session", "codex-spark": "spark-session"}

    view = _compose(_divergent_backends(), receipts, sessions)
    spark = _lanes_by_backend(view)["codex-spark"]

    assert spark["budget_group"] == "spark-sub"
    assert spark["quota_source"] == mcp_views.BORROWED
    assert spark["quota_windows"] == []
    # No percentage reaches the row, so the probed account's figure cannot be
    # read off it as the lane's own position.
    assert "used_percent" not in json.dumps(spark)
    assert "85" not in json.dumps(spark)


def test_the_probed_group_is_not_suppressed() -> None:
    """The suppression is not blanket: a lane in the probed group is unaffected."""

    receipts = {
        "sol-session": _sol_receipt(40),
        "spark-session": _Receipt(CONTEXT_WINDOW, Unmeasured.NO_RATE_LIMITS),
    }
    sessions = {
        "codex": "sol-session",
        "codex-luna": "sol-session",
        "codex-spark": "spark-session",
    }

    lanes = _lanes_by_backend(_compose(_divergent_backends(), receipts, sessions))
    sol = lanes["codex"]

    assert sol["budget_group"] == "codex-sub"
    assert sol["quota_source"] != mcp_views.BORROWED
    assert sol["quota_windows"], "a lane in the probed group must still show a figure"
    assert {row["source"] for row in sol["quota_windows"]} == {"receipt"}

    # Where no sibling diverges the shared probe is adopted outright, so the
    # suppression applies to the divergent lane alone.
    probe_only = {
        "codex": {"launch": "cli", "command": "codex", "budget_group": "codex-sub"},
        "codex-luna": {
            "launch": "cli",
            "command": "codex",
            "budget_group": "codex-sub",
        },
    }
    same_group = _compose(
        probe_only,
        {"sol-session": _sol_receipt(40)},
        {"codex": "sol-session", "codex-luna": "sol-session"},
    )
    adopted = _lanes_by_backend(same_group)["codex"]
    assert adopted["quota_source"] == "probe"
    assert adopted["quota_windows"][0]["used_percent"] == PROBED_USED_PERCENT


def test_a_lane_with_its_reading_of_its_own_is_unaffected() -> None:
    """A divergent lane holding a direct reading keeps that reading."""

    receipts = {
        "spark-session": _sol_receipt(17),
    }
    sessions = {"codex-spark": "spark-session"}

    view = _compose(_divergent_backends(), receipts, sessions)
    spark = _lanes_by_backend(view)["codex-spark"]

    assert spark["budget_group"] == "spark-sub"
    assert spark["quota_source"] == "receipt"
    assert [row["used_percent"] for row in spark["quota_windows"]] == [17]
    assert spark["quota_windows"][0]["used_percent"] != PROBED_USED_PERCENT
