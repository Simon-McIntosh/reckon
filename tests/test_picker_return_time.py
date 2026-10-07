"""Return-time and local-lane figures for the picker state.

The ledger rows and the lane document are synthesised under a temporary
directory, so the module is exercised against the same shapes production reads
without touching the fleet's own ledger or serving document.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context, prompts
from reckon.crew.picker.types import Candidate

NOW = datetime(2026, 10, 2, 12, 1, 0, tzinfo=UTC)
FIVE_KEYS = {"running", "waiting", "admission_verdict", "stale", "slots_state"}
RETURN_TIME_KEYS = {
    "p50_s",
    "p90_s",
    "runs",
    "size_key",
    "size_bucket",
    "budget_source",
    "budget_age_s",
    "stale",
}
CANDIDATE_KEYS = {
    "backend",
    "lane",
    "model",
    "availability",
    "utilisation_pct",
    "burn_multiple",
    "pace_allowance",
    "days_to_reset",
    "resets_at",
    "worker_slots",
    "congestion",
    "outcomes",
    "context",
    "budget_source",
    "budget_age_s",
    "stale",
    "reset_available",
}


def _rows(backend, walls, effort="high", role="implement", spec_level="guided"):
    return [
        {
            "backend": backend,
            "agent": {"effort": effort, "model": "m"},
            "role": role,
            "spec_level": spec_level,
            "wall_seconds": wall,
            "gate": "passed",
            "completed_at": (NOW - timedelta(days=1)).isoformat(),
        }
        for wall in walls
    ]


def _node(time_budget="", role="implement", spec_level="guided"):
    return TaskNode(
        id="n",
        goal="g",
        plan="",
        role=role,
        spec_level=spec_level,
        done_when="d",
        time_budget=time_budget,
    )


def _candidate(backend, effort="high", congestion=None):
    return Candidate(
        backend=backend,
        family="f",
        model="m",
        effort=effort,
        local=False,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=congestion,
        outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    )


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """Point the run-time profile's ledger read at a temporary directory."""

    def synthesise(rows):
        path = tmp_path / "ledger-reckon.json"
        path.write_text(json.dumps({"completed": rows}), encoding="utf-8")
        monkeypatch.setattr(
            "reckon.crew.run_time_profile.ledger.runs",
            lambda *a, **k: json.loads(path.read_text(encoding="utf-8"))["completed"],
        )
        return path

    return synthesise


@pytest.fixture
def isolated_lane(monkeypatch, tmp_path):
    """Point the lane reader at a temporary document for the whole test."""

    path = tmp_path / "lane.json"
    monkeypatch.setenv("RECKON_LOCAL_LANE_DOCUMENT", str(path))
    return path


def test_return_time_figures_and_size_bucket(ledger):
    ledger(_rows("clive", [100, 200, 300, 400]))
    profile = lane_context.run_time_profile("reckon", now=NOW)
    blocks = lane_context.return_times(
        profile, _node(time_budget="45m"), [_candidate("clive")], now=NOW
    )
    block = blocks["clive"]
    assert block["p50_s"] == 250.0
    assert block["p90_s"] == 400.0
    assert block["runs"] == 4
    assert block["size_key"] == "time_budget"
    assert block["size_bucket"] == "30m_to_60m"


def test_group_matches_role_and_specification(ledger):
    rows = _rows("clive", [10, 20]) + _rows(
        "clive", [1000], role="review", spec_level="exact"
    )
    ledger(rows)
    profile = lane_context.run_time_profile("reckon", now=NOW)
    blocks = lane_context.return_times(profile, _node(), [_candidate("clive")], now=NOW)
    assert blocks["clive"]["p50_s"] == 15.0
    assert blocks["clive"]["runs"] == 2


def test_ledger_reading_past_shelf_life_is_stale(ledger):
    ledger([])
    profile = lane_context.run_time_profile("reckon", now=NOW)
    old = (NOW - timedelta(minutes=120)).isoformat()
    snapshot = {
        "backends": [
            {"backend": "clive", "state": {"source": "ledger", "observed_at": old}}
        ]
    }
    blocks = lane_context.return_times(
        profile, _node(), [_candidate("clive")], budget_snapshot=snapshot, now=NOW
    )
    block = blocks["clive"]
    assert block["budget_source"] == "ledger"
    assert block["budget_age_s"] == pytest.approx(7200.0)
    assert block["stale"] is True


def test_fresh_ledger_reading_is_not_stale(ledger):
    ledger([])
    profile = lane_context.run_time_profile("reckon", now=NOW)
    recent = (NOW - timedelta(minutes=5)).isoformat()
    snapshot = {
        "backends": [
            {"backend": "clive", "state": {"source": "ledger", "observed_at": recent}}
        ]
    }
    blocks = lane_context.return_times(
        profile, _node(), [_candidate("clive")], budget_snapshot=snapshot, now=NOW
    )
    assert blocks["clive"]["stale"] is False
    assert blocks["clive"]["budget_age_s"] == pytest.approx(300.0)


def test_account_surface_reading_is_never_stale(ledger):
    ledger([])
    profile = lane_context.run_time_profile("reckon", now=NOW)
    old = (NOW - timedelta(minutes=120)).isoformat()
    snapshot = {
        "backends": [
            {
                "backend": "clive",
                "state": {"source": "account-surface", "observed_at": old},
            }
        ]
    }
    blocks = lane_context.return_times(
        profile, _node(), [_candidate("clive")], budget_snapshot=snapshot, now=NOW
    )
    assert blocks["clive"]["budget_source"] == "account-surface"
    assert blocks["clive"]["stale"] is False


def test_missing_lane_document_is_null(isolated_lane):
    lane = lane_context.local_lane()
    assert lane["running"] is None
    assert lane["waiting"] is None
    assert lane["headroom"] is None
    assert lane["worker_slots"] is None
    assert lane["tokens_per_second"] is None
    assert lane["read_at"] is not None


def test_lane_document_is_read(isolated_lane):
    isolated_lane.write_text(
        json.dumps(
            {
                "observed_at": NOW.isoformat(),
                "running": 7,
                "waiting": 2,
                "headroom": 5,
                "admission": {
                    "worker_slots": 4,
                    "state": "open",
                    "observed_seconds": 300,
                },
            }
        ),
        encoding="utf-8",
    )
    lane = lane_context.local_lane()
    assert lane["running"] == 7
    assert lane["waiting"] == 2
    assert lane["headroom"] == 5
    assert lane["worker_slots"] == 4


def test_representative_nine_candidate_state_is_bounded(ledger, isolated_lane):
    isolated_lane.write_text(
        json.dumps(
            {
                "observed_at": NOW.isoformat(),
                "running": 3,
                "waiting": 9,
                "headroom": 0,
                "admission": {"worker_slots": 0, "state": "congested"},
            }
        ),
        encoding="utf-8",
    )
    ledger(_rows("clive", [200, 400, 900]))
    congestion = {
        "running": 3,
        "waiting": 9,
        "admission_verdict": "congested",
        "stale": False,
        "slots_state": "congested",
    }
    assert set(congestion) == FIVE_KEYS
    candidates = [_candidate("clive", congestion=congestion)]
    candidates += [_candidate(f"backend-{index}") for index in range(8)]
    rendered = prompts.render(
        "state.jinja",
        node=_node(time_budget="45m"),
        capability={},
        estimated_context=0,
        comment="size the state",
        candidates=candidates,
        project="reckon",
        now=NOW,
    )
    assert len(rendered) <= 1500 * 5
    payload = json.loads(rendered)
    assert set(payload["candidates"]["clive"]) == CANDIDATE_KEYS
    assert set(payload["return_times"]["clive"]) == RETURN_TIME_KEYS
    assert payload["return_times"]["clive"]["p50_s"] == 400.0
    assert payload["local_lane"]["running"] == 3
    assert payload["local_lane"]["waiting"] == 9
