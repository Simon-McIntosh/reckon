"""Dispatch candidates use the same recorded account window as a direct pick."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reckon.crew import paid_lanes, picker, window_reading
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, lane_context, snapshot

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


def _freeze_snapshot_clock(monkeypatch, moment):
    """Pin the picker snapshot's wall clock, so its age is measured from ``moment``.

    ``snapshot`` reads ``datetime.now(UTC)`` for a budget reading's age, and the
    test's own ``now`` is a second read of the same clock. Two reads that
    straddle a second — or a host that pauses between them — make the measured
    age drift from the recorded observation, which is what made the
    stale-window case fail intermittently. Pinning the module's clock leaves a
    single source for both.
    """

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(snapshot, "datetime", _FrozenDatetime)


def _receipt_record(observed, reset, *, used_percent=2.0):
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
                    "used_percent": used_percent,
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
    reset = now + timedelta(days=7) - timedelta(hours=12)
    records = [_receipt_record(now, reset, used_percent=6.0)]
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    monkeypatch.setattr(snapshot.budget._backends, "probe_budget", lambda **_k: {})
    config = _config()
    dispatched = dispatch._picker_budget_snapshot("demo", config, tmp_path, records)
    direct = snapshot.budget_view("demo", config, tmp_path, records)
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
        assert actual["burn_multiple"] == pytest.approx(0.84, rel=0.02)
        assert actual["utilisation_pct"] == pytest.approx(6.0)
        assert dispatched["backends"][0]["state"]["source"] == "ledger"


def test_missing_recorded_window_names_absence_without_inventing_figures(
    monkeypatch, tmp_path
):
    config = _config()
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    paid_lanes.write_document_atomically(
        paid_lanes.compose_document(("codex", "codex-astra"), moment=datetime.now(UTC))
    )
    view = dispatch._picker_budget_snapshot("demo", config, tmp_path, [])
    _candidates(monkeypatch, tmp_path, config, [], view)
    selection = _dispatch_selection(monkeypatch, tmp_path, config, [], view)
    candidates = {
        candidate["backend"]: candidate
        for candidate in [*selection["offered"], *selection["excluded"]]
    }
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
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _freeze_snapshot_clock(monkeypatch, now)
    observed = now - timedelta(days=1)
    records = [
        _receipt_record(
            observed, now + timedelta(days=6) - timedelta(hours=12), used_percent=6.0
        )
    ]
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    config = _config()
    view = dispatch._picker_budget_snapshot("demo", config, tmp_path, records)
    candidates = _candidates(monkeypatch, tmp_path, config, records, view)
    for backend in view["backends"]:
        assert backend["state"]["source"] == "ledger"
        assert backend["state"]["expired"] is True
        assert backend["state"]["observed_at"] == observed.isoformat()
        budget_block = lane_context._budget_block(
            backend["state"], moment=now, shelf_life_minutes=30
        )
        assert budget_block["stale"] is True
        assert budget_block["budget_age_s"] == pytest.approx(86_400, abs=1)
    for candidate in candidates.values():
        # A stale reading keeps its figures and carries its age; it does not
        # null the numbers, which would hide the account whenever it is idle.
        assert candidate["burn_multiple"] is not None
        assert candidate["pace_allowance"] is not None
        assert candidate["utilisation_pct"] is not None
        assert candidate["stale"] is True
        assert candidate["budget_age_s"] == pytest.approx(86_400, abs=1)
        assert candidate["budget_reason"] is None
        assert candidate["reasons"] == []


def test_dispatch_uses_published_window_as_direct_pick_does(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    reset = now + timedelta(days=7) - timedelta(hours=12)
    reading = window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="seven_day",
                utilisation=0.06,
                observed_at=now,
                age_seconds=0.0,
                resets_at=reset.isoformat(),
            ),
        ),
        observed_at=now,
        age_seconds=0.0,
    )
    config = _config()
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    monkeypatch.setattr(snapshot.budget._backends, "probe_budget", lambda **_k: {})
    direct = snapshot.budget_view(
        "demo", config, tmp_path, [_receipt_record(now, reset, used_percent=6.0)]
    )
    expected = _candidates(monkeypatch, tmp_path, config, [], direct)
    document = paid_lanes.compose_document(
        ["codex"],
        sources={"codex": [paid_lanes.Candidate("rollout", reading)]},
        moment=now,
    )
    paid_lanes.write_document_atomically(document)
    dispatched = dispatch._picker_budget_snapshot("demo", config, tmp_path, [])
    actual = _candidates(monkeypatch, tmp_path, config, [], dispatched)
    for name in ("codex", "codex-astra"):
        for field in ("burn_multiple", "pace_allowance", "utilisation_pct"):
            assert actual[name][field] is not None
            assert actual[name][field] == pytest.approx(expected[name][field], rel=0.01)
        assert actual[name]["budget_reason"] is None
        assert dispatched["backends"][0]["state"]["observed_at"] == now.isoformat()
        budget_block = lane_context._budget_block(
            dispatched["backends"][0]["state"],
            moment=now,
            shelf_life_minutes=30,
        )
        assert budget_block["budget_age_s"] == pytest.approx(0, abs=1)
        assert budget_block["stale"] is False


def test_dispatch_marks_old_published_window_stale(monkeypatch, tmp_path):
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    _freeze_snapshot_clock(monkeypatch, now)
    observed = now - timedelta(hours=2)
    reading = window_reading.WindowReading(
        figures=(
            window_reading.WindowFigure(
                period="seven_day",
                utilisation=0.42,
                observed_at=observed,
                age_seconds=7200.0,
                resets_at=(now + timedelta(days=3)).isoformat(),
            ),
        ),
        observed_at=observed,
        age_seconds=7200.0,
    )
    paid_lanes.write_document_atomically(
        paid_lanes.compose_document(
            ["codex"],
            sources={"codex": [paid_lanes.Candidate("rollout", reading)]},
            moment=now,
        )
    )
    config = _config()
    monkeypatch.setattr(snapshot.budget.crew, "list_live", list)
    view = dispatch._picker_budget_snapshot("demo", config, tmp_path, [])
    candidates = _candidates(monkeypatch, tmp_path, config, [], view)
    budget_block = lane_context._budget_block(
        view["backends"][0]["state"], moment=now, shelf_life_minutes=30
    )
    assert budget_block["budget_age_s"] == pytest.approx(7200, abs=1)
    assert budget_block["stale"] is True
    for candidate in candidates.values():
        # A stale published reading reaches the candidate with its figures and
        # age, marked stale, rather than nulled with a reason.
        assert candidate["burn_multiple"] is not None
        assert candidate["pace_allowance"] is not None
        assert candidate["stale"] is True
        assert candidate["budget_age_s"] == pytest.approx(7200, abs=1)
        assert candidate["budget_reason"] is None
