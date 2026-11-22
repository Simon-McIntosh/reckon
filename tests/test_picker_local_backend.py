"""Automatic offers use the configured local harness and respect its reservation."""

import copy
import json

import pytest

from reckon import crew_dispatch_commands, flight
from reckon.crew import dispatch_picker
from reckon.crew.picker import snapshot
from tests.test_picker_reserved_lanes import (
    CONFIG,
    HARDEST,
    _dispatch_pick,
    _invoke,
    _payload,
    _request,
)

pytest_plugins = ("tests.test_picker_reserved_lanes",)


@pytest.fixture
def local_flight(tmp_path):
    config = copy.deepcopy(CONFIG)
    config["local_backend"] = "omp-local"
    config["backends"].update(
        {
            "omp-local": {
                "launch": "cli",
                "command": "omp",
                "model": "local/local-model",
            },
            "clive": {"launch": "cli", "command": "clive", "model": "local-model"},
            "clive-spare": {
                "launch": "cli",
                "command": "clive",
                "model": "local-model",
            },
        }
    )
    endpoints = tmp_path / "endpoints.json"
    endpoints.write_text(json.dumps({"endpoints": [{"model_id": "local-model"}]}))
    for name in ("omp-local", "clive", "clive-spare"):
        config["backends"][name]["endpoints_document"] = str(endpoints)
    host = tmp_path / "flight.yaml"
    host.write_text(json.dumps(config))
    return flight.resolve(
        host_path=host,
        project_path=tmp_path / "no-project.yaml",
        catalogue_path=tmp_path / "no-catalogue.yaml",
    ).config


def _view(config):
    return {
        "groups": [],
        "backends": [
            {
                "backend": name,
                "held": False,
                "state": {
                    "headroom": "known",
                    "utilisation_pct": 20.0,
                    "expired": False,
                },
            }
            for name in config["backends"]
        ],
    }


def _options(root, config, request):
    return snapshot.candidates(
        request,
        config,
        root,
        records=[],
        verdict_inputs={},
        budget_snapshot=_view(config),
    )


def test_an_ordinary_node_gets_only_the_configured_local_harness(
    live_facts, local_flight
):
    options = _options(live_facts, local_flight, _request())
    offered = {c.backend for c in options if not c.reasons}
    print("ordinary offered:", sorted(offered))
    print("excluded:", {c.backend: c.reasons for c in options if c.reasons})
    assert "omp-local" in offered
    assert "codex-astra" in offered
    assert offered.isdisjoint({"clive", "clive-spare", "claude-opus"})
    claude = next(c for c in options if c.backend == "claude-opus")
    assert any(reason.startswith("reserved-lane:") for reason in claude.reasons)


def test_a_reserved_tier_gets_the_claude_lane(live_facts, local_flight):
    options = _options(live_facts, local_flight, _request(capability=HARDEST))
    offered = {c.backend for c in options if not c.reasons}
    assert {"omp-local", "claude-opus"} <= offered
    assert offered.isdisjoint({"clive", "clive-spare"})


def test_an_explicit_local_wrapper_choice_is_kept(repo, monkeypatch, local_flight):
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: local_flight
    )

    def never(*a, **k):
        raise AssertionError("an explicit backend must bypass automatic offering")

    monkeypatch.setattr(dispatch_picker, "dispatch_picker_selection", never)
    result = _invoke(repo, "--backend", "clive", "--comment", "explicit harness choice")
    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["route"] == "deterministic"
    assert payload["backend"] == payload["requested_backend"] == "clive"


@pytest.mark.parametrize("role", ["implement", "review"])
def test_selecting_a_local_wrapper_does_not_bypass_its_reservation(
    live_facts, local_flight, role
):
    local_flight["local_backend"] = "clive"
    options = _options(live_facts, local_flight, _request(role=role))
    selected = next(c for c in options if c.backend == "clive")
    assert selected.local
    assert selected.reservation["family"] == "claude"
    assert any(reason.startswith("reserved-lane:") for reason in selected.reasons)


def test_a_local_backend_can_name_a_derived_entry(live_facts, local_flight):
    local_flight["backends"]["omp-local"]["derived_from"] = "lanes.host.models.local"
    options = _options(live_facts, local_flight, _request())
    assert any(c.backend == "omp-local" and not c.reasons for c in options)


@pytest.mark.parametrize(
    "command,model,served",
    [
        ("omp", "local/local-model", True),
        ("omp", "local/another-model", False),
        ("codex", "local/local-model", False),
        ("claude", "local/local-model", False),
    ],
)
def test_endpoint_matching_uses_the_harness_checkpoint_identifier(
    local_flight, command, model, served
):
    backend = {
        **local_flight["backends"]["omp-local"],
        "command": command,
        "model": model,
    }
    assert (flight._probe_serving(backend)["serving"] == "serving") is served


def test_a_local_wrapper_exception_records_its_policy_and_reason(
    live_facts, local_flight, monkeypatch
):
    local_flight["local_backend"] = "clive"
    reason = "declared deep reasoning and critical risk justify the reserved harness"
    selection, calls = _dispatch_pick(
        live_facts,
        monkeypatch,
        _request(capability=HARDEST),
        [("local:local-model:clive", reason)],
        assessment=HARDEST["requirements"],
        config=local_flight,
        snapshot=_view(local_flight),
    )
    assert selection["backend"] == "clive"
    assert selection["reserved_lane"] == "claude"
    assert selection["reason"] == reason
    assert len(calls) == 1


def test_a_claude_reask_also_excludes_the_selected_local_wrapper(local_flight):
    local_flight["local_backend"] = "clive"
    filtered = dispatch_picker._config_without_lane(local_flight, "claude")
    assert {"clive", "clive-spare", "claude-opus"}.isdisjoint(filtered["backends"])
    assert "omp-local" in filtered["backends"]
