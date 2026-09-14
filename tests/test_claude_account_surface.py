"""The claude dialect reads its own quota from the account surface.

The dialect owns credential, transport and strict parse: every way the read can
fail folds to an unknown block naming the reason, so a shape change or an
expired login leaves headroom unknown rather than plausibly low.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import _backends, mcp_views

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backends"
CLAUDE_SETTINGS = {"launch": "cli", "command": "claude"}


def _recorded_answer() -> dict[str, Any]:
    with (FIXTURES / "claude-account-limits.json").open() as handle:
        return json.load(handle)


def _missing_credential() -> dict[str, Any]:
    raise FileNotFoundError("no stored credential")


def _live_credential(now: datetime) -> dict[str, Any]:
    return {
        "accessToken": "live-token",
        "refreshToken": "live-refresh",
        "refreshTokenExpiresAt": int((now + timedelta(days=30)).timestamp()),
        "expiresAt": int((now + timedelta(hours=1)).timestamp()),
    }


def _read(
    payload: object,
    *,
    now: datetime | None = None,
    credential: dict[str, Any] | None = None,
) -> dict[str, Any]:
    composed_at = now or datetime(2030, 1, 1, tzinfo=UTC)
    if credential is None:
        credential = _live_credential(composed_at)
    current = _backends._load_claude_credential
    _backends._load_claude_credential = lambda: credential
    try:
        return _backends.probe_budget(
            backend_name="metered",
            backend=CLAUDE_SETTINGS,
            fetch=lambda oauth: payload,
            now=composed_at,
        )
    finally:
        _backends._load_claude_credential = current


def test_recorded_answer_yields_utilisation_and_reset_time() -> None:
    answer = _recorded_answer()
    budget = _read(answer)

    window = answer["windows"]["weekly"]
    assert budget["headroom"] == "known"
    assert budget["utilisation_pct"] == round(100.0 * window["utilization"], 1)
    assert budget["resets_at"] == _backends._epoch_to_iso(window["resetsAt"])
    assert budget["rate_limit_type"] == "weekly"
    assert _backends.budget_exhausted(budget) is False


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"windows": "not-a-mapping"},
        {"windows": {}},
        {"unrelated": "shape"},
    ],
)
def test_unrecognised_shape_is_unknown_never_low(payload: object) -> None:
    budget = _read(payload)

    assert budget["headroom"] == "unknown"
    assert budget["utilisation_pct"] is None
    assert "account answer" in budget["detail"]


def test_missing_named_window_is_unknown() -> None:
    answer = _recorded_answer()
    del answer["windows"]["weekly"]
    budget = _read(answer)

    assert budget["headroom"] == "unknown"
    assert budget["utilisation_pct"] is None
    assert "weekly" in budget["detail"]


@pytest.mark.parametrize("bad", ["high", True, None, [0.89]])
def test_non_numeric_utilisation_is_unknown(bad: object) -> None:
    answer = _recorded_answer()
    answer["windows"]["weekly"]["utilization"] = bad
    budget = _read(answer)

    assert budget["headroom"] == "unknown"
    assert budget["utilisation_pct"] is None
    assert "not numeric" in budget["detail"]


def test_expired_credential_is_unknown_naming_the_reason(monkeypatch) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    expired = {
        "accessToken": "expired-token",
        "refreshToken": "expired-refresh",
        "refreshTokenExpiresAt": int((now - timedelta(hours=1)).timestamp()),
    }

    def _transport_must_not_run(oauth):
        raise AssertionError("the transport must not run past an expired login")

    monkeypatch.setattr(_backends, "_load_claude_credential", lambda: expired)
    budget = _backends.probe_budget(
        backend_name="metered",
        backend=CLAUDE_SETTINGS,
        fetch=_transport_must_not_run,
        now=now,
    )

    assert budget["headroom"] == "unknown"
    assert budget["utilisation_pct"] is None
    assert budget["resets_at"] is None
    assert "expired" in budget["detail"].lower()
    assert _backends.budget_exhausted(budget) is None


def test_unreadable_credential_store_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(_backends, "_load_claude_credential", _missing_credential)
    budget = _backends.probe_budget(
        backend_name="metered",
        backend=CLAUDE_SETTINGS,
        fetch=lambda oauth: _recorded_answer(),
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert budget["headroom"] == "unknown"
    assert "credential" in budget["detail"].lower()


def test_lanes_view_reports_a_declared_probe_without_executing_one(
    monkeypatch,
) -> None:
    def _must_not_execute(*args, **kwargs):
        raise AssertionError("a declared probe must not be executed")

    monkeypatch.setattr(_backends, "probe_budget", _must_not_execute)
    command, note = mcp_views._declared_probe_command(
        {"launch": "cli", "command": "codex"}
    )

    assert command == "codex"
    assert note == "quota probe declared"
