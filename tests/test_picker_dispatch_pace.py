"""Dispatch candidates use the same recorded account window as a direct pick."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reckon.crew import picker
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot

dispatch = importlib.import_module("reckon.crew.dispatch")


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
            done_when="Candidates have budget figures",
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


def _dispatch_selection(monkeypatch, tmp_path, config, records, view):
    def pick(request, settings, *, repo, **inputs):
        options = snapshot.candidates(
            request,
            settings,
            repo,
            records=inputs["records"],
            budget_snapshot=inputs["budget_snapshot"],
            cached_only=True,
        )
        return SimpleNamespace(
            as_dict=lambda: {
                "action": "route",
                "offered": [item.as_dict() for item in options if not item.reasons],
                "excluded": [item.as_dict() for item in options if item.reasons],
            }
        )

    monkeypatch.setattr(picker, "pick", pick)
    return dispatch.dispatch_picker_selection(
        node=_request().node,
        config=config,
        project="demo",
        repo=tmp_path,
        records=records,
        verdict_inputs={},
        budget_snapshot=view,
    )


def test_dispatch_candidates_match_direct_pick_from_recorded_window(
    monkeypatch, tmp_path
):
    now = datetime.now(UTC)
    reset = now + timedelta(days=7) - timedelta(hours=2)
    records = [_receipt_record(now, reset)]
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    config = _config()
    dispatched = dispatch._picker_budget_snapshot("demo", config, tmp_path, records)
    direct = snapshot.budget_view("demo", config, tmp_path, records, cached_only=True)
    _candidates(monkeypatch, tmp_path, config, records, dispatched)
    selection = _dispatch_selection(monkeypatch, tmp_path, config, records, dispatched)
    dispatched_candidates = {
        candidate["backend"]: candidate
        for candidate in [*selection["offered"], *selection["excluded"]]
    }
    direct_candidates = _candidates(monkeypatch, tmp_path, config, records, direct)
    fields = (
        "burn_multiple",
        "pace_allowance",
        "utilisation_pct",
        "days_to_reset",
        "resets_at",
    )
    for name in ("codex", "codex-astra"):
        actual = dispatched_candidates[name]
        expected = direct_candidates[name]
        for field in fields:
            assert actual[field] is not None, (name, field, actual)
            if isinstance(actual[field], float):
                assert actual[field] == pytest.approx(expected[field], rel=0.01)
            else:
                assert actual[field] == expected[field]
        assert actual["burn_multiple"] == pytest.approx(1.68, rel=0.02)
        assert actual["utilisation_pct"] == pytest.approx(2.0)
        assert dispatched["backends"][0]["state"]["source"] == "ledger"


def test_missing_recorded_window_names_absence_without_inventing_figures(
    monkeypatch, tmp_path
):
    config = _config()
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    view = dispatch._picker_budget_snapshot("demo", config, tmp_path, [])
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
        assert "no recorded account-window reading" in candidate["budget_reason"]
        assert candidate["reasons"] == []


def test_old_recorded_window_is_marked_stale(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    observed = now - timedelta(days=1)
    records = [_receipt_record(observed, now + timedelta(days=6))]
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    config = _config()
    view = dispatch._picker_budget_snapshot("demo", config, tmp_path, records)
    candidates = _candidates(monkeypatch, tmp_path, config, records, view)
    for backend in view["backends"]:
        assert backend["state"]["source"] == "ledger"
        assert backend["state"]["expired"] is True
        assert backend["state"]["observed_at"] == observed.isoformat()
    for candidate in candidates.values():
        assert candidate["burn_multiple"] is None
        assert candidate["pace_allowance"] is None
        assert "stale" in candidate["budget_reason"]
        assert candidate["reasons"] == []
