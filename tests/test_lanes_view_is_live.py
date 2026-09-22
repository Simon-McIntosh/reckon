"""The lanes view prefers attributable live quota probes over old receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reckon import _backends, mcp_views

SHORT_WINDOW_MINUTES = 5 * 60
LONG_WINDOW_MINUTES = 7 * 24 * 60
SHARED_SHORT_RESET = 1_893_484_800
SHARED_LONG_RESET = 1_894_089_600
SEPARATE_LONG_RESET = SHARED_LONG_RESET + 4 * 60 * 60


@dataclass(frozen=True)
class _Reading:
    window_minutes: int
    used_percent: int
    resets_at: int


@dataclass(frozen=True)
class _Receipt:
    model_context_window: int
    quota_readings: dict[int, _Reading]


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _receipt(short_used: int, long_used: int, long_reset: int) -> _Receipt:
    return _Receipt(
        model_context_window=128_000,
        quota_readings={
            SHORT_WINDOW_MINUTES: _Reading(
                SHORT_WINDOW_MINUTES, short_used, SHARED_SHORT_RESET
            ),
            LONG_WINDOW_MINUTES: _Reading(LONG_WINDOW_MINUTES, long_used, long_reset),
        },
    )


def _probe_block(short_used: int = 95, long_used: int = 37) -> dict[str, Any]:
    return {
        "headroom": "known",
        "quota_windows": {
            SHORT_WINDOW_MINUTES: {
                "window_minutes": SHORT_WINDOW_MINUTES,
                "used_percent": short_used,
                "resets_at": _backends._epoch_to_iso(SHARED_SHORT_RESET),
            },
            LONG_WINDOW_MINUTES: {
                "window_minutes": LONG_WINDOW_MINUTES,
                "used_percent": long_used,
                "resets_at": _backends._epoch_to_iso(SHARED_LONG_RESET),
            },
        },
        "detail": "account quota probe answered",
    }


def _view(
    probe_reader,
    *,
    composed_at: datetime | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = composed_at or datetime(2030, 1, 1, tzinfo=UTC)
    ancient = now - timedelta(hours=4)
    selected_config = config or {
        "budget": {"evidence_shelf_life_minutes": 1},
        "backends": {
            "shared": {"launch": "cli", "command": "codex"},
            "separate": {"launch": "cli", "command": "codex"},
        },
    }
    receipts = {
        "shared-session": _receipt(11, 22, SHARED_LONG_RESET),
        "separate-session": _receipt(73, 43, SEPARATE_LONG_RESET),
        "plain-session": _receipt(13, 23, SHARED_LONG_RESET),
    }
    sessions = {
        "shared": "shared-session",
        "separate": "separate-session",
        "plain": "plain-session",
    }
    runs = [
        {
            "backend": backend,
            "session_id": sessions[backend],
            "completed_at": _stamp(ancient),
        }
        for backend in selected_config["backends"]
    ]
    return mcp_views.crew_lanes_view(
        selected_config,
        runs,
        receipt_reader=receipts.__getitem__,
        probe_reader=probe_reader,
        composed_at=_stamp(now),
    )


def _lanes(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lane["backend"]: lane for lane in view["lanes"]}


def _windows(lane: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {window["window_minutes"]: window for window in lane["quota_windows"]}


def test_live_probe_replaces_an_ancient_receipt_only_for_the_lane_it_describes():
    composed_at = datetime(2030, 1, 1, tzinfo=UTC)
    lanes = _lanes(
        _view(lambda backend, settings: _probe_block(), composed_at=composed_at)
    )

    shared = _windows(lanes["shared"])
    separate = _windows(lanes["separate"])

    assert lanes["shared"]["probe_status"] == "answered"
    assert lanes["shared"]["quota_source"] == "probe"
    assert {window["source"] for window in shared.values()} == {"probe"}
    assert {window["observed_at"] for window in shared.values()} == {
        _stamp(composed_at)
    }
    assert {window["age_seconds"] for window in shared.values()} == {0}
    assert all(
        window["serving_state"] != mcp_views.STALE_SERVING_STATE
        for window in shared.values()
    )

    assert lanes["separate"]["probe_status"] == "unmatched"
    assert lanes["separate"]["quota_source"] == "receipt"
    assert {window["source"] for window in separate.values()} == {"receipt"}
    assert all(window["age_seconds"] == 4 * 60 * 60 for window in separate.values())
    assert separate[LONG_WINDOW_MINUTES]["used_percent"] == 43


def test_probe_windows_keep_their_lengths_and_values() -> None:
    short_used = 96
    long_used = 38
    shared = _windows(
        _lanes(_view(lambda backend, settings: _probe_block(short_used, long_used)))[
            "shared"
        ]
    )

    assert set(shared) == {SHORT_WINDOW_MINUTES, LONG_WINDOW_MINUTES}
    assert shared[SHORT_WINDOW_MINUTES]["used_percent"] == short_used
    assert shared[LONG_WINDOW_MINUTES]["used_percent"] == long_used
    assert shared[LONG_WINDOW_MINUTES]["used_percent"] != short_used


@pytest.mark.parametrize(
    "probe_reader",
    [
        pytest.param(
            lambda backend, settings: (_ for _ in ()).throw(RuntimeError("boom")),
            id="raises",
        ),
        pytest.param(lambda backend, settings: None, id="returns-nothing"),
    ],
)
def test_probe_failure_falls_back_to_the_receipt_and_says_it_did_not_answer(
    probe_reader,
) -> None:
    lane = _lanes(_view(probe_reader))["shared"]

    assert lane["probe_status"] == "unavailable"
    assert "probe did not answer" in lane["probe_detail"]
    assert lane["quota_source"] == "receipt"
    assert {window["source"] for window in lane["quota_windows"]} == {"receipt"}


def test_two_reads_inside_the_declared_cache_lifetime_probe_once() -> None:
    invocations = 0

    def probe_reader(backend, settings):
        nonlocal invocations
        invocations += 1
        return _probe_block()

    first = datetime(2030, 1, 1, tzinfo=UTC)
    _view(probe_reader, composed_at=first)
    second = _view(probe_reader, composed_at=first + timedelta(seconds=30))

    assert invocations == 1
    assert _lanes(second)["shared"]["probe_cached"] is True


def test_a_dialect_without_a_probe_never_calls_the_probe_reader() -> None:
    """A command with no readable surface leaves the reader uninvoked.

    The command names a harness reckon cannot translate, so the composer
    declares no probe for it and the reader's seam is never entered.  This case
    once used ``claude`` as its stand-in for a probe-less dialect; that stopped
    being true when the claude dialect took over its own account-surface read,
    so ``claude`` became a probe carrier and the fixture no longer described the
    absence this case exists to assert.
    """
    invocations = 0

    def probe_reader(backend, settings):
        nonlocal invocations
        invocations += 1
        return _probe_block()

    lane = _lanes(
        _view(
            probe_reader,
            config={
                "backends": {
                    "plain": {"launch": "cli", "command": "ollama"},
                }
            },
        )
    )["plain"]

    assert invocations == 0
    assert lane["probe_status"] == "not_declared"
    assert lane["quota_source"] == "receipt"


@pytest.mark.parametrize(
    ("used_percent", "expected"),
    [
        (mcp_views.AT_RISK_USED_PERCENT, mcp_views.AT_RISK_SERVING_STATE),
        (100, mcp_views.EXHAUSTED_SERVING_STATE),
    ],
)
def test_live_probe_keeps_the_existing_risk_and_exhaustion_states(
    used_percent: int, expected: str
) -> None:
    lane = _lanes(
        _view(lambda backend, settings: _probe_block(short_used=used_percent))
    )["shared"]

    assert _windows(lane)[SHORT_WINDOW_MINUTES]["serving_state"] == expected


def test_codex_probe_parser_retains_every_window_keyed_by_length() -> None:
    answer = {
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 29,
                    "windowDurationMins": SHORT_WINDOW_MINUTES,
                    "resetsAt": SHARED_SHORT_RESET,
                },
                "secondary": {
                    "usedPercent": 71,
                    "windowDurationMins": LONG_WINDOW_MINUTES,
                    "resetsAt": SHARED_LONG_RESET,
                },
            }
        }
    }

    block = _backends.probe_budget(
        backend_name="metered",
        backend={"launch": "cli", "command": "codex"},
        runner=lambda probe: answer,
    )

    assert set(block["quota_windows"]) == {
        SHORT_WINDOW_MINUTES,
        LONG_WINDOW_MINUTES,
    }
    assert block["quota_windows"][SHORT_WINDOW_MINUTES]["used_percent"] == 29
    assert block["quota_windows"][LONG_WINDOW_MINUTES]["used_percent"] == 71


def _lane_for_run(run: dict[str, Any], *, composed_at: datetime) -> dict[str, Any]:
    """Compose the lanes view over one run, entering where a caller enters.

    The entry point is the composer rather than the dating helper, because a
    direct call proves the function and not the wiring: a lane can only be shown
    to report a stamp if the view that publishes it is the thing asked.
    """
    view = mcp_views.crew_lanes_view(
        {"backends": {"solo": {"launch": "cli", "command": "codex"}}},
        [run],
        receipt_reader=lambda session_id: _receipt(11, 22, SHARED_LONG_RESET),
        probe_reader=lambda backend, settings: None,
        composed_at=_stamp(composed_at),
    )
    return _lanes(view)["solo"]


def test_the_lane_dates_a_run_whose_only_stamp_is_created_at() -> None:
    """A record dated only by created_at is datable, and the lane publishes it.

    The field list this view kept used to stop before created_at, so a record
    whose only stamp is that one rendered unmeasured here while the pace reader
    dated the same record. The same stamp decides which of a backend's sessions
    is the newest, so the two surfaces could disagree about which run a figure
    came from.
    """
    composed = datetime(2030, 1, 1, tzinfo=UTC)
    created = composed - timedelta(hours=2)

    lane = _lane_for_run(
        {
            "backend": "solo",
            "session_id": "sess-created-at",
            "created_at": _stamp(created),
        },
        composed_at=composed,
    )

    assert lane["receipt_state"] == "readable"
    assert lane["observed_at"] == _stamp(created)
    # A lane with no date carries the refusal under this key; a lane that
    # resolved one carries no refusal at all, which is the distinction asserted.
    assert lane.get("unmeasured", {}).get("observed_at") is None


def test_the_lane_passes_over_a_budget_stamp_it_cannot_parse() -> None:
    """A malformed budget stamp loses to the readable stamp behind it.

    The shared helper parses before it returns, so a value that is present but
    unreadable is passed over rather than published: the lane dates the run by
    its next readable stamp. A helper that returned the first truthy field would
    hand the malformed text back as a date instead.
    """
    composed = datetime(2030, 1, 1, tzinfo=UTC)
    completed = composed - timedelta(hours=3)

    lane = _lane_for_run(
        {
            "backend": "solo",
            "session_id": "sess-malformed-stamp",
            "budget": {"observed_at": "yesterday afternoon"},
            "completed_at": _stamp(completed),
        },
        composed_at=composed,
    )

    assert lane["observed_at"] == _stamp(completed)
    assert lane["observed_at"] != "yesterday afternoon"
    assert lane.get("unmeasured", {}).get("observed_at") is None
