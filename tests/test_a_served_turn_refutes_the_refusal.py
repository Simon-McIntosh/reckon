"""A later served turn invalidates an earlier refusal in the same stream.

The streams are constructed because their range of orderings is the point:
rejection followed by service, silence, another rejection, or mere prose must
remain distinguishable.  A provider-side ``rate_limit_event`` with status
``allowed`` or ``allowed_warning`` is the served-turn evidence; text that talks
about a rejection is not an event and a rejected retry is not recovery.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import budget

NOW = datetime(2026, 9, 6, 18, 30, tzinfo=UTC)
CONFIG = {
    "backends": {"worker": {"launch": "cli", "command": "claude"}},
    "budget": {
        "utilisation_ceiling_pct": 95,
        "resume_reserve_pct": 0,
        "exhausted_statuses": ["exhausted"],
    },
}


def _rate_limit(status: str) -> dict:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": status},
    }


def _timestamped_message(kind: str, text: str, moment: datetime) -> dict:
    return {
        "type": kind,
        "timestamp": moment.isoformat().replace("+00:00", "Z"),
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _write_stream(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(f"{json.dumps(event)}\n" for event in events))
    return path


def _verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    events: list[dict],
    *,
    resets_at: str | None,
) -> dict:
    observed_at = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    block = {
        "headroom": "known",
        "utilisation_pct": 100.0,
        "threshold_status": "exhausted",
        "rate_limit_type": "usage-limit",
        "resets_at": resets_at,
        "refusal": True,
        "detail": "backend refused the turn: the account's usage-limit is reached",
    }
    pointer = {
        "run_id": "refused-run",
        "project": "project",
        "backend": "worker",
        "budget": block,
        "created_at": observed_at,
        "observed_at": observed_at,
        "log_path": str(_write_stream(tmp_path / "stream.jsonl", events)),
    }
    monkeypatch.setattr(budget.crew, "list_live", lambda: [pointer])
    monkeypatch.setattr(
        budget.ledger,
        "load",
        lambda _project, _root: ({"runs": [], "members": []}, 1),
    )
    return budget.preflight(
        "project",
        CONFIG,
        backends=["worker"],
        root=tmp_path,
        now=NOW,
    )["backends"][0]


def _refusal_stream(*following: dict) -> list[dict]:
    return [
        _timestamped_message(
            "assistant", "work before the refusal", NOW - timedelta(minutes=6)
        ),
        _rate_limit("rejected"),
        _timestamped_message(
            "assistant", "the refused request ended", NOW - timedelta(minutes=5)
        ),
        *following,
    ]


def test_a_later_served_turn_refutes_the_newest_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verdict = _verdict(
        monkeypatch,
        tmp_path,
        _refusal_stream(_rate_limit("allowed")),
        resets_at=(NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
    )

    assert verdict["held"] is False
    assert verdict["state"]["headroom"] == "unknown"
    assert "served turn" in verdict["reason"]
    assert "refuted" in verdict["reason"]


@pytest.mark.parametrize("served_status", ["allowed", "allowed_warning"])
def test_each_served_status_refutes_the_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, served_status: str
) -> None:
    verdict = _verdict(
        monkeypatch,
        tmp_path,
        _refusal_stream(_rate_limit(served_status)),
        resets_at=(NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
    )

    assert verdict["held"] is False
    assert served_status in verdict["reason"]


def test_a_rejection_followed_by_nothing_still_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verdict = _verdict(
        monkeypatch,
        tmp_path,
        _refusal_stream(),
        resets_at=None,
    )

    assert verdict["held"] is True
    assert verdict["state"]["age_source"] == "rate-limit-event"


def test_a_retry_storm_is_not_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verdict = _verdict(
        monkeypatch,
        tmp_path,
        _refusal_stream(_rate_limit("rejected"), _rate_limit("rejected")),
        resets_at=(NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
    )

    assert verdict["held"] is True


def test_a_future_reset_without_service_keeps_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verdict = _verdict(
        monkeypatch,
        tmp_path,
        _refusal_stream(),
        resets_at=(NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
    )

    assert verdict["held"] is True
    assert verdict["reason"] == (
        "backend reports threshold status 'exhausted', which policy counts as "
        "exhausted regardless of utilisation; utilisation 100% with burn "
        "multiple unknown"
    )


def test_a_past_reset_remains_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verdict = _verdict(
        monkeypatch,
        tmp_path,
        _refusal_stream(),
        resets_at=(NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
    )

    assert verdict["held"] is False
    assert verdict["state"]["expired"] is True
    assert "measured window reset" in verdict["reason"]


def test_prose_that_mentions_a_rejection_is_not_a_rejection(tmp_path: Path) -> None:
    stream = _write_stream(
        tmp_path / "prose.jsonl",
        [
            _timestamped_message(
                "user", "inspect rate_limit_event status rejected", NOW
            ),
            _timestamped_message(
                "assistant", "the fixture says the request was rejected", NOW
            ),
        ],
    )

    assert budget._refusal_event_stamp({"log_path": str(stream)}) is None
