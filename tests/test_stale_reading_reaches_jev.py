"""A stale account-window reading reaches Jev with its figures and age.

A reading past its shelf life is not an absent one. Nulling its burn and pace
hides a lane's figures exactly when the account has been idle, which is when a
router most needs them. These tests build the dispatch-path candidate over a
two-hour-old codex reading and assert the figures, the age and the stale flag
travel together; a genuinely absent reading still yields nulls and a reason.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import paid_lanes, window_reading
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot

dispatch = importlib.import_module("reckon.crew.dispatch")


@pytest.fixture(autouse=True)
def isolated_crew_home(monkeypatch, tmp_path):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))


def _config():
    return {
        "backends": {
            name: {
                "launch": "cli",
                "command": "codex",
                "model": name,
                "budget_group": "codex-sub",
                "budget_check": True,
            }
            for name in ("codex", "codex-astra")
        },
        "roles": {"implement": {}},
        "budget": {"pace_multiple": 1.1},
    }


def _request():
    return PickRequest(
        "demo",
        TaskNode(
            id="example",
            goal="Exercise the picker",
            plan="",
            role="implement",
            spec_level="guided",
            done_when="Candidates carry the stale reading",
            estimated_hours=0.5,
        ),
    )


def _candidates(monkeypatch, tmp_path, config, records, view):
    monkeypatch.setattr(snapshot, "_fit", lambda *_a, **_k: [])
    monkeypatch.setattr(snapshot, "_dispatch_lane_gate", lambda *_a: {"state": "open"})
    monkeypatch.setattr(
        snapshot, "_serving_observation", lambda *_a: {"status": "served"}
    )
    monkeypatch.setattr(snapshot.routing, "shared_verdict_inputs", lambda *_a: {})
    monkeypatch.setattr(snapshot.routing, "_estimated_hours", lambda *_a: 0.5)
    return {
        candidate.backend: candidate.as_dict()
        for candidate in snapshot.candidates(
            _request(),
            config,
            tmp_path,
            records=records,
            budget_snapshot=view,
            cached_only=True,
        )
    }


def _receipt_record(observed, reset):
    return {
        "run_id": "sample-run",
        "backend": "codex",
        "completed_at": observed.isoformat(),
        "lane_receipt": {
            "quota_state": "measured",
            "observed_at": observed.isoformat(),
            "quota_windows": [
                {
                    "window_minutes": 10_080,
                    "used_percent": 2.0,
                    "resets_at": int(reset.timestamp()),
                    "observed_at": observed.isoformat(),
                }
            ],
        },
    }


def _published_view(now, observed):
    reading = window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="seven_day",
                utilisation=0.42,
                observed_at=observed,
                age_seconds=(now - observed).total_seconds(),
                resets_at=(now + timedelta(days=3)).isoformat(),
            ),
        ),
        observed_at=observed,
        age_seconds=(now - observed).total_seconds(),
    )
    paid_lanes.write_document_atomically(
        paid_lanes.compose_document(
            ["codex"],
            sources={"codex": [paid_lanes.Candidate("rollout", reading)]},
            moment=now,
        )
    )


def _dispatch_view(config, tmp_path, records):
    return dispatch._picker_budget_snapshot("demo", config, tmp_path, records)


def test_stale_recorded_reading_reaches_candidate_with_figures_and_age(
    monkeypatch, tmp_path
):
    now = datetime.now(UTC)
    observed = now - timedelta(hours=2)
    records = [_receipt_record(observed, now + timedelta(days=6))]
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    monkeypatch.setattr(snapshot.budget._backends, "probe_budget", lambda **_k: {})
    config = _config()
    view = _dispatch_view(config, tmp_path, records)
    candidates = _candidates(monkeypatch, tmp_path, config, records, view)
    for name in ("codex", "codex-astra"):
        candidate = candidates[name]
        assert candidate["burn_multiple"] is not None, candidate
        assert candidate["pace_allowance"] is not None, candidate
        assert candidate["utilisation_pct"] is not None
        assert candidate["days_to_reset"] is not None
        assert candidate["resets_at"] is not None
        assert candidate["stale"] is True
        assert candidate["budget_age_s"] == pytest.approx(7200, abs=5)
        assert candidate["budget_reason"] is None
        assert candidate["reasons"] == []


def test_stale_published_reading_reaches_candidate_with_figures_and_age(
    monkeypatch, tmp_path
):
    now = datetime.now(UTC)
    observed = now - timedelta(hours=2)
    _published_view(now, observed)
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    config = _config()
    view = _dispatch_view(config, tmp_path, [])
    candidates = _candidates(monkeypatch, tmp_path, config, [], view)
    for candidate in candidates.values():
        assert candidate["burn_multiple"] is not None, candidate
        assert candidate["pace_allowance"] is not None, candidate
        assert candidate["utilisation_pct"] is not None
        assert candidate["stale"] is True
        assert candidate["budget_age_s"] == pytest.approx(7200, abs=5)
        assert candidate["budget_reason"] is None


def test_absent_reading_stays_null_with_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    config = _config()
    paid_lanes.write_document_atomically(
        paid_lanes.compose_document(("codex", "codex-astra"), moment=datetime.now(UTC))
    )
    view = _dispatch_view(config, tmp_path, [])
    candidates = _candidates(monkeypatch, tmp_path, config, [], view)
    for candidate in candidates.values():
        assert all(
            candidate[field] is None
            for field in (
                "burn_multiple",
                "pace_allowance",
                "utilisation_pct",
                "days_to_reset",
                "resets_at",
            )
        )
        assert candidate["stale"] is None
        assert "no recorded account-window reading" in candidate["budget_reason"]


def test_fresh_reading_is_unchanged(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    reset = now + timedelta(days=7) - timedelta(hours=2)
    records = [_receipt_record(now, reset)]
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    monkeypatch.setattr(snapshot.budget._backends, "probe_budget", lambda **_k: {})
    config = _config()
    view = _dispatch_view(config, tmp_path, records)
    candidates = _candidates(monkeypatch, tmp_path, config, records, view)
    for name in ("codex", "codex-astra"):
        candidate = candidates[name]
        assert candidate["burn_multiple"] == pytest.approx(1.68, rel=0.02)
        assert candidate["utilisation_pct"] == pytest.approx(2.0)
        assert candidate["pace_allowance"] is not None
        assert candidate["stale"] is False
        assert candidate["budget_age_s"] == pytest.approx(0, abs=5)
        assert candidate["budget_reason"] is None
        assert candidate["reasons"] == []
