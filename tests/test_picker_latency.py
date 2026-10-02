"""One project read and one budget snapshot serve every candidate of a pick."""

from unittest.mock import Mock

import pytest

from reckon import budget, ledger
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, pick, snapshot
from reckon.crew.picker.replay import _summary


@pytest.fixture
def request_node():
    return PickRequest(
        "example",
        TaskNode(
            id="parser",
            goal="Implement a parser with clear validation errors",
            plan="",
            role="implement",
            spec_level="guided",
            done_when="Parser tests pass",
            estimated_hours=0.5,
        ),
        capability={"class": "general", "requirements": {"verification": "strict"}},
        estimated_context=32000,
        comment="",
    )


def nine_candidate_config():
    """A config whose candidate count is nine, as the pick's fixture declares."""
    remote = {
        "launch": "cli",
        "command": "codex",
        "model": "remote-model",
        "effort": "high",
    }
    backends = {
        "local": {
            "launch": "cli",
            "command": "clive",
            "model": "local-model",
            "effort": "high",
        },
    }
    for index in range(8):
        backends[f"remote-{index}"] = dict(remote, model=f"remote-model-{index}")
    return {
        "default_backend": "local",
        "local_backend": "local",
        "budget": {"pace_multiple": 1.1},
        "roles": {"implement": {}},
        "backends": backends,
    }


@pytest.fixture
def live_facts(monkeypatch):
    """Stub the live probes, keeping the competence path that reads the ledger."""
    monkeypatch.setattr(snapshot.budget, "recorded_windows", lambda *a, **k: {})
    monkeypatch.setattr(snapshot.budget, "group_pace", lambda *a, **k: [])
    monkeypatch.setattr(
        snapshot.budget, "state_for", lambda name, *a, **k: budget.BudgetState(name)
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda *a, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        snapshot.routing,
        "_context_fit_verdict",
        lambda **k: {"allowed": True, "window_tokens": 100000},
    )
    monkeypatch.setattr(
        snapshot,
        "budget_view",
        lambda *a, **k: {"backends": [], "groups": []},
    )
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )
    # The capability cache and the project ledger live outside the worktree; the
    # counter stands in so the read is observed without leaving the repository.
    monkeypatch.setattr(
        snapshot.routing.capabilities,
        "load_capabilities",
        lambda *a, **k: {
            "configurations": [],
            "routing": {"rows": []},
            "ledger_versions": {"example": "v0"},
        },
    )


def answer(choice):
    return {
        "model": "answering-snapshot",
        "answers": {
            "route": {
                "choice": choice,
                "confidence": 0.9,
                "probabilities": {"local": 0.2, "remote-0": 0.8},
            }
        },
    }


def run_pick(request_node, config, tmp_path):
    return pick(
        request_node,
        config,
        repo=tmp_path,
        records=[],
        caller=lambda *a, **k: answer("remote-0"),
    )


def test_ledger_loader_runs_once_per_pick(
    live_facts, monkeypatch, request_node, tmp_path
):
    config = nine_candidate_config()
    loads = Mock(return_value=({}, 0))
    monkeypatch.setattr(ledger, "load", loads)
    monkeypatch.setattr(ledger, "history_version", lambda data, version: "v0")

    selection = run_pick(request_node, config, tmp_path)

    assert len(selection.offered) == 9
    assert loads.call_count == 1


def test_budget_snapshot_is_taken_once_per_pick(
    live_facts, monkeypatch, request_node, tmp_path
):
    config = nine_candidate_config()
    view = Mock(return_value={"backends": [], "groups": []})
    monkeypatch.setattr(snapshot, "budget_view", view)

    run_pick(request_node, config, tmp_path)

    assert view.call_count == 1


def test_shared_read_does_not_change_verdicts(
    live_facts, monkeypatch, request_node, tmp_path
):
    config = nine_candidate_config()
    loads = Mock(return_value=({}, 0))
    monkeypatch.setattr(ledger, "load", loads)
    monkeypatch.setattr(ledger, "history_version", lambda data, version: "v0")

    shared = snapshot.routing.shared_verdict_inputs(request_node.project, tmp_path)
    unshared_reads = loads.call_count

    shared_reasons = [
        snapshot._fit(request_node, name, backend, tmp_path, verdict_inputs=shared)
        for name, backend in config["backends"].items()
    ]
    per_candidate_reasons = [
        snapshot._fit(request_node, name, backend, tmp_path)
        for name, backend in config["backends"].items()
    ]

    assert shared_reasons == per_candidate_reasons
    assert len(shared_reasons) == 9
    # The unshared arm reads the ledger once per candidate; the shared arm adds none.
    assert loads.call_count - unshared_reads == 9


def _row(backend, model, jev_latency_ms, latency_ms=100.0):
    return {
        "actual_backend": backend,
        "selection": {
            "backend": backend,
            "model": model,
            "fallback_reason": None,
            "latency_ms": latency_ms,
            "jev_latency_ms": jev_latency_ms,
            "usage": {},
        },
    }


def test_replay_jev_median_reads_only_rows_that_called_jev():
    rows = [
        _row("remote-0", "remote-model-0", 250.0),
        _row("remote-1", "remote-model-1", 350.0),
        _row("local", "local-model", 0.0),
    ]
    summary = _summary(rows, {}, 3, 0.0)
    assert summary["jev_calls"] == 2
    assert summary["median_jev_latency_ms"] == 300.0


def test_replay_counts_a_picked_model_its_observation_refuses():
    rows = [_row("remote-0", "remote-model-0", 100.0)]
    observations = {("remote-0", "remote-model-0"): {"status": "refused"}}
    summary = _summary(rows, observations, 1, 0.0)
    assert summary["refused_model_picks"] == 1
