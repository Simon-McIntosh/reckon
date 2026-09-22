"""The crew lanes view exposes measurements without making a routing choice."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reckon import _backends, mcp, mcp_views
from reckon._mcp_tools import CrewArgs
from reckon.crew.rollout import Unmeasured
from reckon.flight import resolve


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

    def _receipt(session_id: str, **_kwargs: Any) -> Any:
        return lane_fixture["receipts"][session_id]

    monkeypatch.setattr(
        mcp_views.rollout_module,
        "read_rollout_receipt",
        _receipt,
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


class _RaisingSurfaceDialect(_backends.Dialect):
    """A dialect whose own reading would raise, so any execution is fatal."""

    name = "raising-surface"

    def read_account_surface(self, **_kwargs) -> dict[str, Any] | None:
        raise AssertionError("the account reading must not run")


def test_declaration_never_executes_an_owned_account_surface_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialect = _RaisingSurfaceDialect()
    monkeypatch.setattr(mcp_views._backends, "dialect_for", lambda settings: dialect)

    command, detail = mcp_views._declared_probe_command(
        {"launch": "cli", "command": "claude"}
    )

    assert command == "claude"
    assert detail == "account-surface probe declared"


def test_an_owned_account_surface_is_reported_declared_without_reading_it() -> None:
    def raise_reader(backend_name: str, settings: Mapping[str, Any]) -> None:
        raise AssertionError("the account reading must not run")

    view = mcp_views.crew_lanes_view(
        {"backends": {"plain": {"launch": "cli", "command": "claude"}}},
        [
            {
                "backend": "plain",
                "session_id": "plain-session",
                "completed_at": "2030-01-02T03:04:05Z",
            }
        ],
        receipt_reader=lambda session: SimpleNamespace(
            model_context_window=None,
            quota_readings=Unmeasured.MISSING_ROLLOUT,
        ),
        probe_reader=raise_reader,
        composed_at="2030-01-02T03:05:06Z",
    )
    lane = view["lanes"][0]

    assert lane["probe_status"] == "unavailable"
    assert "the account reading must not run" in lane["probe_detail"]


def test_a_dialect_declaring_nothing_stays_not_declared_and_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations = 0

    def probe_reader(backend_name: str, settings: Mapping[str, Any]) -> None:
        nonlocal invocations
        invocations += 1

    monkeypatch.setattr(
        mcp_views._backends,
        "dialect_for",
        lambda settings: _backends.Dialect(),
    )
    view = mcp_views.crew_lanes_view(
        {"backends": {"plain": {"launch": "cli", "command": "no-surface"}}},
        [],
        probe_reader=probe_reader,
        composed_at="2030-01-02T03:05:06Z",
    )

    assert invocations == 0
    assert view["lanes"][0]["probe_status"] == "not_declared"


def test_two_backends_sharing_one_command_resolve_to_a_single_read() -> None:
    invocations = 0

    def probe_reader(backend_name: str, settings: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal invocations
        invocations += 1
        return {
            "quota_windows": {
                300: {
                    "window_minutes": 300,
                    "used_percent": 50,
                    "resets_at": "2030-01-03T03:04:05Z",
                }
            },
            "detail": "account quota probe answered",
        }

    backends = {
        name: {"launch": "cli", "command": "claude"} for name in ("left", "right")
    }
    view = mcp_views.crew_lanes_view(
        {"backends": backends},
        [],
        probe_reader=probe_reader,
        composed_at="2030-01-02T03:05:06Z",
    )
    lanes = _lanes_by_backend(view)

    assert invocations == 1
    assert lanes["left"]["probe_status"] == "answered"
    assert lanes["right"]["probe_status"] == "answered"


BUDGET_WEEKLY_MINUTES = 7 * 24 * 60
BUDGET_FIVE_HOUR_MINUTES = 5 * 60
BUDGET_WEEKLY_RESET = 1_906_732_800
BUDGET_FIVE_HOUR_RESET = BUDGET_WEEKLY_RESET - BUDGET_FIVE_HOUR_MINUTES * 60


def _budget_receipt(short_used: int, weekly_used: int) -> _Receipt:
    return _Receipt(
        BUDGET_WEEKLY_MINUTES * 12 + BUDGET_FIVE_HOUR_MINUTES * 2,
        {
            BUDGET_FIVE_HOUR_MINUTES: _Reading(
                BUDGET_FIVE_HOUR_MINUTES, short_used, BUDGET_FIVE_HOUR_RESET
            ),
            BUDGET_WEEKLY_MINUTES: _Reading(
                BUDGET_WEEKLY_MINUTES, weekly_used, BUDGET_WEEKLY_RESET
            ),
        },
    )


def _budget_probe_block() -> dict[str, Any]:
    return {
        "headroom": "known",
        "quota_windows": {
            BUDGET_FIVE_HOUR_MINUTES: {
                "window_minutes": BUDGET_FIVE_HOUR_MINUTES,
                "used_percent": 30,
                "resets_at": _backends._epoch_to_iso(BUDGET_FIVE_HOUR_RESET),
            },
            BUDGET_WEEKLY_MINUTES: {
                "window_minutes": BUDGET_WEEKLY_MINUTES,
                "used_percent": 40,
                "resets_at": _backends._epoch_to_iso(BUDGET_WEEKLY_RESET),
            },
        },
        "detail": "account quota probe answered",
    }


def _compose_budget_view(
    backends: dict[str, dict[str, Any]],
    receipts: dict[str, _Receipt],
    *,
    probe_reader: Any,
) -> dict[str, Any]:
    runs = [
        {
            "run_id": f"run-{name}",
            "backend": name,
            "session_id": f"{name}-session",
            "completed_at": "2030-01-02T03:04:05Z",
        }
        for name in backends
    ]
    return mcp_views.crew_lanes_view(
        {"backends": backends},
        runs,
        receipt_reader=receipts.__getitem__,
        probe_reader=probe_reader,
        composed_at="2030-01-02T03:05:06Z",
    )


def test_shared_probe_is_borrowed_where_declared_pools_diverge() -> None:
    receipts = {
        "sol-session": _budget_receipt(30, 40),
        "luna-session": _budget_receipt(30, 40),
        "spark-session": _Receipt(
            BUDGET_WEEKLY_MINUTES * 12 + BUDGET_FIVE_HOUR_MINUTES * 2, {}
        ),
    }
    backends = {
        "sol": {"launch": "cli", "command": "codex", "budget_group": "codex-main"},
        "luna": {"launch": "cli", "command": "codex", "budget_group": "codex-main"},
        "spark": {"launch": "cli", "command": "codex", "budget_group": "spark"},
    }
    probes = 0

    def probe_reader(_backend: str, _settings: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal probes
        probes += 1
        return _budget_probe_block()

    view = _compose_budget_view(backends, receipts, probe_reader=probe_reader)
    lanes = _lanes_by_backend(view)

    assert probes == 1
    sol = lanes["sol"]
    spark = lanes["spark"]

    assert sol["quota_source"] == "receipt"
    assert sol["probe_status"] == "answered"
    assert {window["source"] for window in sol["quota_windows"]} == {"receipt"}
    assert _windows_by_length(sol)[BUDGET_WEEKLY_MINUTES]["used_percent"] == 40

    assert spark["quota_source"] == "borrowed"
    assert spark["quota_windows"] == []
    assert spark["unmeasured"]["quota_windows"] == "shared_command_probe_not_owned"


def test_backends_declaring_the_same_pool_carry_the_shared_probe_as_their_own() -> None:
    receipts = {
        "left-session": _budget_receipt(30, 40),
        "right-session": _budget_receipt(30, 40),
    }
    backends = {
        "left": {"launch": "cli", "command": "codex", "budget_group": "shared-budget"},
        "right": {"launch": "cli", "command": "codex", "budget_group": "shared-budget"},
    }
    probes = 0

    def probe_reader(_backend: str, _settings: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal probes
        probes += 1
        return _budget_probe_block()

    view = _compose_budget_view(backends, receipts, probe_reader=probe_reader)
    lanes = _lanes_by_backend(view)

    assert probes == 1
    assert lanes["left"]["quota_source"] == "probe"
    assert lanes["right"]["quota_source"] == "probe"
    assert {window["source"] for window in lanes["left"]["quota_windows"]} == {"probe"}
    assert view["budget_groups"] == {"shared-budget": ["left", "right"]}


def test_identical_reset_schedules_never_group_without_a_declared_pool() -> None:
    receipts = {
        "sol-session": _budget_receipt(30, 40),
        "luna-session": _budget_receipt(30, 40),
        "spark-session": _budget_receipt(30, 40),
        "terra-session": _budget_receipt(30, 40),
    }
    backends = {
        "sol": {"launch": "cli", "command": "codex", "budget_group": "codex-main"},
        "luna": {"launch": "cli", "command": "codex", "budget_group": "codex-main"},
        "spark": {"launch": "cli", "command": "codex", "budget_group": "spark"},
        "terra": {"launch": "cli", "command": "codex"},
    }
    view = _compose_budget_view(
        backends,
        receipts,
        probe_reader=lambda _backend, _settings: _budget_probe_block(),
    )

    budget_groups = view["budget_groups"]
    assert budget_groups == {"codex-main": ["luna", "sol"], "spark": ["spark"]}
    assert "terra" not in budget_groups["codex-main"]
    assert "spark" not in budget_groups["codex-main"]


def _write_flight_layer(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_declared_group_in_flight_config_reaches_the_lanes_view(tmp_path) -> None:
    """The slot a shipped layer declares is the one the lanes view reads: the
    grouping a reader consults (``crew_lanes_view``) surfaces ``budget_group``
    straight from resolved flight config, one pool per declared group and an
    undeclared backend left ungrouped."""
    shipped = _write_flight_layer(
        tmp_path / "shipped" / "flight.yaml",
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: probe-cli\n"
        "    budget_group: pool-a\n"
        "  beta:\n"
        "    launch: cli\n"
        "    command: probe-cli\n"
        "    budget_group: pool-a\n"
        "  terra:\n"
        "    launch: cli\n"
        "    command: probe-cli\n",
    )
    config = resolve(
        shipped_path=shipped, host_path=tmp_path / "absent" / "flight.yaml"
    ).config

    view = mcp_views.crew_lanes_view(
        config,
        [],
        probe_reader=lambda _backend, _settings: _budget_probe_block(),
        composed_at="2030-01-02T03:05:06Z",
    )
    lanes = _lanes_by_backend(view)

    assert view["budget_groups"] == {"pool-a": ["alpha", "beta"]}
    assert lanes["alpha"]["budget_group"] == "pool-a"
    assert lanes["beta"]["budget_group"] == "pool-a"
    assert lanes["terra"]["budget_group"] is None
    assert "terra" not in view["budget_groups"]["pool-a"]


def test_dead_spelling_groups_nothing_even_when_the_value_matches() -> None:
    """``quota_pool`` declares no group: a backend carrying it stays ungrouped
    even when the value names a live pool verbatim, so the view groups on the
    schema's one spelling and a reader-side alias to the dead key would break
    this test rather than pass silently."""
    backends = {
        "live": {"launch": "cli", "command": "probe-cli", "budget_group": "pool-a"},
        "stale": {"launch": "cli", "command": "probe-cli", "quota_pool": "pool-a"},
    }
    view = mcp_views.crew_lanes_view(
        {"backends": backends},
        [],
        probe_reader=lambda _backend, _settings: _budget_probe_block(),
        composed_at="2030-01-02T03:05:06Z",
    )
    lanes = _lanes_by_backend(view)

    assert view["budget_groups"] == {"pool-a": ["live"]}
    assert lanes["live"]["budget_group"] == "pool-a"
    assert lanes["stale"]["budget_group"] is None


# ── A cached account reading carries its fetch age beside the figure ─────────

CACHE_WEEKLY_MINUTES = 7 * 24 * 60
CACHE_WEEKLY_RESET = 1_789_416_000


def _cache_payload(*, with_stamp: bool, stamp: object = None) -> dict[str, Any]:
    """An on-disk copy of the account block, carried on the probe cache path."""
    payload = {
        "windows": {
            "weekly": {
                "utilization": 0.53,
                "resetsAt": CACHE_WEEKLY_RESET,
                "windowMinutes": CACHE_WEEKLY_MINUTES,
            },
        },
    }
    if with_stamp:
        payload["fetch_stamp"] = stamp
    return payload


def _cache_sourced_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cache_payload: dict[str, Any],
    composed_at: str = "2030-01-02T03:05:06Z",
) -> dict[str, Any]:
    """Compose the lanes view through its production probe reader, with an
    on-disk account cache and a dead live surface, so the cache is the only
    source the probe path can answer from."""
    cache = tmp_path / "account-cache.json"
    cache.write_text(json.dumps(cache_payload))
    composed = datetime(2030, 1, 2, 3, 5, 6, tzinfo=UTC)
    monkeypatch.setattr(
        _backends,
        "_load_claude_credential",
        lambda: {
            "accessToken": "live-token",
            "refreshTokenExpiresAt": int((composed + timedelta(days=30)).timestamp()),
        },
    )
    monkeypatch.setattr(
        _backends,
        "_fetch_claude_account",
        lambda credential: (_ for _ in ()).throw(OSError("surface down")),
    )
    weekly = _Reading(CACHE_WEEKLY_MINUTES, 40, CACHE_WEEKLY_RESET)
    receipts = {
        "metered-session": _Receipt(
            CACHE_WEEKLY_MINUTES * 12, {CACHE_WEEKLY_MINUTES: weekly}
        )
    }
    runs = [
        {
            "run_id": "run-metered",
            "backend": "metered",
            "session_id": "metered-session",
            "completed_at": "2030-01-02T03:04:05Z",
        }
    ]
    return mcp_views.crew_lanes_view(
        {"backends": {"metered": {"launch": "cli", "command": "claude"}}},
        runs,
        receipt_reader=receipts.__getitem__,
        composed_at=composed_at,
        account_cache_path=cache,
    )


def _figure_without_age(view: object) -> list[dict]:
    """Structures that surface the cache's own stamp without its fetch age.

    The stamp is the cache's provenance marker, so the moment it appears on a
    lane the age must be there too: a bare stamp would mean a figure with its
    age dropped. A live reading legitimately carries neither and is not
    flagged.
    """
    found: list[dict] = []
    stack: list[object] = [view]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "probe_fetch_stamp" in node and "probe_fetch_age_seconds" not in node:
                found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def test_a_cached_reading_carries_its_fetch_age_beside_the_figure_in_the_view(
    tmp_path, monkeypatch
) -> None:
    """The fixture copy is five hours old at composition, so the age beside the
    figure is five hours (18000 seconds) — derived from the fixture, never a
    hardcoded age literal."""
    composed = datetime(2030, 1, 2, 3, 5, 6, tzinfo=UTC)
    cache_age_hours = 5
    view = _cache_sourced_view(
        tmp_path,
        monkeypatch,
        cache_payload=_cache_payload(
            with_stamp=True,
            stamp=int((composed - timedelta(hours=cache_age_hours)).timestamp()),
        ),
    )
    lane = _lanes_by_backend(view)["metered"]
    window = _windows_by_length(lane)[CACHE_WEEKLY_MINUTES]

    assert lane["probe_status"] == "answered"
    assert lane["quota_source"] == "probe"
    assert lane["probe_fetch_age_seconds"] == cache_age_hours * 3600
    assert window["source"] == "probe"
    assert window["used_percent"] == 53.0
    assert window["age_seconds"] == cache_age_hours * 3600
    assert window["observed_at"] == lane["probe_fetch_stamp"]
    assert window["serving_state"] == mcp_views.STALE_SERVING_STATE
    assert _figure_without_age(view) == []


@pytest.mark.parametrize(
    "cache_payload",
    [
        pytest.param(
            _cache_payload(with_stamp=False),
            id="stamp-key-absent",
        ),
        pytest.param(
            _cache_payload(with_stamp=True, stamp="monday-ish"),
            id="stamp-unparseable",
        ),
    ],
)
def test_an_unresolvable_fetch_stamp_is_unknown_not_a_figure(
    tmp_path, monkeypatch, cache_payload
) -> None:
    """A copy whose fetch stamp cannot be resolved never renders the figure.

    This is a refusal: if any figure leaked from the unresolvable copy the
    assertion below would fail, because the only figure the copy carries (53)
    must not appear anywhere in the view.
    """
    view = _cache_sourced_view(tmp_path, monkeypatch, cache_payload=cache_payload)
    lane = _lanes_by_backend(view)["metered"]

    assert lane["probe_status"] == "unavailable"
    assert "probe_fetch_age_seconds" not in lane
    assert "probe_fetch_stamp" not in lane
    assert [row["used_percent"] for row in lane["quota_windows"]] == [40]
    assert _figure_without_age(view) == []
