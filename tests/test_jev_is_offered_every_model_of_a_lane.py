"""A declared lane offers each model once, regardless of legacy names."""

from pathlib import Path

import pytest

from reckon import flight
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot


@pytest.mark.parametrize("legacy_name", [False, True])
def test_declared_lane_offers_every_model_once(monkeypatch, tmp_path, legacy_name):
    overrides = {
        "lanes": {
            "codex": {
                "launch": "cli",
                "command": "codex",
                "default_model": "sol",
                "models": {key: {"model": key} for key in ("sol", "astra", "luna")},
            }
        }
    }
    if legacy_name:
        overrides["backends"] = {"codex-astra": {"launch": "cli"}}
    absent = tmp_path / "absent.yaml"
    config = flight.resolve(
        overrides=overrides,
        host_path=absent,
        project_path=absent,
        catalogue_path=absent,
    ).config
    assert ("codex-astra" in config["backends"]) is legacy_name
    monkeypatch.setattr(snapshot.routing, "_estimated_hours", lambda *a: 1)
    monkeypatch.setattr(
        snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
    )
    monkeypatch.setattr(snapshot.routing, "_context_fit_verdict", lambda **k: None)
    monkeypatch.setattr(snapshot, "_dispatch_lane_gate", lambda *a: {"state": "open"})
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda *a, **k: {"status": "served"},
    )
    request = PickRequest(
        "example",
        TaskNode(
            id="work",
            goal="work",
            plan="",
            role="implement",
            spec_level="guided",
            done_when="done",
        ),
        capability={},
        estimated_context=100,
    )
    offered = snapshot.candidates(
        request,
        config,
        Path(tmp_path),
        records=[],
        verdict_inputs={},
        budget_snapshot={"backends": [], "groups": []},
    )
    codex = [item for item in offered if item.family == "codex"]
    assert {f"{item.family}:{item.model}" for item in codex} == {
        "codex:sol",
        "codex:astra",
        "codex:luna",
    }
    assert len(codex) == 3
    assert {item.backend for item in codex} == {"codex"}
