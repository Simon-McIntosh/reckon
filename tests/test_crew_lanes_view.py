"""The crew lanes view exposes measurements without making a routing choice."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reckon import mcp, mcp_views
from reckon._mcp_tools import CrewArgs
from reckon.crew.rollout import Unmeasured


@dataclass(frozen=True)
class _Reading:
    window_minutes: int
    used_percent: int | float | Unmeasured
    resets_at: int | float | Unmeasured


@dataclass(frozen=True)
class _Receipt:
    model_context_window: int | Unmeasured
    quota_readings: dict[int, _Reading] | Unmeasured


@pytest.fixture()
def lane_fixture() -> dict[str, Any]:
    five_hour_minutes = 5 * 60
    weekly_minutes = 7 * 24 * 60
    short_used = 100 - len("remain")
    weekly_used = 6 * 7
    weekly_reset = weekly_minutes * 10**5
    short_reset = weekly_reset - weekly_minutes
    context_window = weekly_minutes * 12 + five_hour_minutes * 2
    observed_at = "2030-01-02T03:04:05Z"
    composed_at = "2030-01-02T03:05:06Z"
    zero_used = len(())

    weekly = _Reading(weekly_minutes, weekly_used, weekly_reset)
    short = _Reading(five_hour_minutes, short_used, short_reset)
    zero = _Reading(five_hour_minutes, zero_used, short_reset)
    receipts = {
        "weekly-first": _Receipt(
            context_window,
            {weekly_minutes: weekly, five_hour_minutes: short},
        ),
        "weekly-second": _Receipt(
            context_window,
            {five_hour_minutes: short, weekly_minutes: weekly},
        ),
        "missing": _Receipt(
            Unmeasured.MISSING_ROLLOUT,
            Unmeasured.MISSING_ROLLOUT,
        ),
        "zero": _Receipt(context_window, {five_hour_minutes: zero}),
        "quota-less": _Receipt(context_window, Unmeasured.NO_RATE_LIMITS),
    }
    backend_sessions = {
        "alpha": "weekly-first",
        "beta": "weekly-second",
        "gamma": "missing",
        "delta": "zero",
        "epsilon": "quota-less",
    }
    backends = {
        name: {"alias": f"alias-{name}", "model": f"model-{name}"}
        for name in (*backend_sessions, "idle")
    }
    runs = [
        {
            "run_id": f"run-{index}",
            "backend": backend,
            "session_id": session,
            "completed_at": observed_at,
        }
        for index, (backend, session) in enumerate(backend_sessions.items())
    ]
    return {
        "config": {"backends": backends},
        "runs": runs,
        "receipts": receipts,
        "five_hour_minutes": five_hour_minutes,
        "weekly_minutes": weekly_minutes,
        "short_used": short_used,
        "weekly_used": weekly_used,
        "context_window": context_window,
        "observed_at": observed_at,
        "composed_at": composed_at,
        "zero_used": zero_used,
    }


def _compose(fixture: dict[str, Any]) -> dict[str, Any]:
    return mcp_views.crew_lanes_view(
        fixture["config"],
        fixture["runs"],
        receipt_reader=fixture["receipts"].__getitem__,
        composed_at=fixture["composed_at"],
    )


def _lanes_by_backend(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["backend"]: row for row in view["lanes"]}


def _windows_by_length(lane: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {row["window_minutes"]: row for row in lane["quota_windows"]}


def test_quota_horizons_are_keyed_by_length_not_receipt_position(
    lane_fixture: dict[str, Any],
) -> None:
    view = _compose(lane_fixture)
    lanes = _lanes_by_backend(view)
    weekly_minutes = lane_fixture["weekly_minutes"]
    five_hour_minutes = lane_fixture["five_hour_minutes"]
    weekly_used = lane_fixture["weekly_used"]
    short_used = lane_fixture["short_used"]

    alpha = _windows_by_length(lanes["alpha"])
    beta = _windows_by_length(lanes["beta"])

    assert alpha[weekly_minutes] == beta[weekly_minutes]
    assert alpha[weekly_minutes]["used_percent"] == weekly_used
    assert alpha[five_hour_minutes]["used_percent"] == short_used
    assert alpha[weekly_minutes]["used_percent"] != short_used
    assert set(alpha) == {five_hour_minutes, weekly_minutes}


def test_each_lane_carries_context_and_observation_times(
    lane_fixture: dict[str, Any],
) -> None:
    view = _compose(lane_fixture)
    lanes = _lanes_by_backend(view)
    alpha = lanes["alpha"]

    assert view["composed_at"] == lane_fixture["composed_at"]
    assert alpha["observed_at"] == lane_fixture["observed_at"]
    assert alpha["effective_context_window"] == lane_fixture["context_window"]
    assert all(
        window["observed_at"] == lane_fixture["observed_at"]
        for window in alpha["quota_windows"]
    )


def test_missing_unreadable_unused_and_measured_zero_stay_distinct(
    lane_fixture: dict[str, Any],
) -> None:
    lanes = _lanes_by_backend(_compose(lane_fixture))
    missing = lanes["gamma"]
    zero = _windows_by_length(lanes["delta"])[lane_fixture["five_hour_minutes"]]
    idle = lanes["idle"]
    quota_less = lanes["epsilon"]

    assert missing["receipt_state"] == "unreadable"
    assert missing["effective_context_window"] == "unmeasured"
    assert missing["quota_windows"] == []
    assert missing["unmeasured"]["receipt"] == str(Unmeasured.MISSING_ROLLOUT.value)

    assert idle["receipt_state"] == "unused"
    assert idle["quota_windows"] == []
    assert idle != missing

    assert zero["used_percent"] == lane_fixture["zero_used"]
    assert zero["remaining_percent"] == 100 - lane_fixture["zero_used"]
    assert zero["serving_state"] == "will_serve"
    assert zero["used_percent"] != missing["effective_context_window"]

    assert quota_less["receipt_state"] == "readable"
    assert quota_less["quota_windows"] == []
    assert quota_less["unmeasured"]["quota_windows"] == str(
        Unmeasured.NO_RATE_LIMITS.value
    )


def test_view_returns_every_configured_backend_and_no_routing_choice(
    lane_fixture: dict[str, Any],
) -> None:
    view = _compose(lane_fixture)

    assert {row["backend"] for row in view["lanes"]} == set(
        lane_fixture["config"]["backends"]
    )
    assert all("serving_state" not in row for row in view["lanes"])
    returned = json.dumps(view).lower()
    for forbidden in ("chosen", "best", "recommended", "preferred"):
        assert forbidden not in returned


def test_crew_tool_accepts_lanes_and_keeps_unknown_view_refusal(
    lane_fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(
        mcp.flight_module,
        "resolve",
        lambda *args, **kwargs: SimpleNamespace(config=lane_fixture["config"]),
    )
    monkeypatch.setattr(
        mcp.ledger_module,
        "runs",
        lambda *args, **kwargs: lane_fixture["runs"],
    )
    monkeypatch.setattr(
        mcp.flight_module,
        "mounted_project_docs",
        lambda: {"proj": repository / "docs"},
    )
    monkeypatch.setattr(mcp.crew_module, "list_live", list)
    monkeypatch.setattr(
        mcp_views.rollout_module,
        "read_rollout_receipt",
        lane_fixture["receipts"].__getitem__,
    )

    args = CrewArgs(project="proj", view="lanes", checkout_path=str(repository))
    result = mcp._crew(**args.model_dump())
    rejected = mcp._crew("proj", view="not-a-view", checkout_path=str(repository))

    assert result["ok"] is True
    assert result["view"] == "lanes"
    assert len(result["lanes"]) == len(lane_fixture["config"]["backends"])
    assert rejected["ok"] is False
    assert rejected["error"] == "invalid_view"


def test_tool_description_names_both_horizons_and_disclaims_recommendation() -> None:
    description = (mcp._crew.__doc__ or "").lower()

    assert "five-hour" in description
    assert "weekly" in description
    assert "before choosing a lane" in description
    assert "never selects" in description
    assert "recommends" in description
