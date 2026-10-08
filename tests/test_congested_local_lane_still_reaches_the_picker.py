"""A busy local lane is judged after the dispatch routing choice."""

import copy
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import picker
from tests import test_dispatch_names_its_backend as base

pytest_plugins = ("tests.test_dispatch_names_its_backend",)


@pytest.mark.parametrize("choice", ["beta", "alpha", "hold", "explicit"])
def test_congested_local_lane_routing(dispatch_repo, tmp_path, monkeypatch, choice):
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": -1,
                "sizing_verdict": "congested",
                "admission": {
                    "observed_seconds": 300,
                    "worker_slots": 0,
                    "sessions": {"session": {"worker_slots": 0}},
                },
            }
        )
    )
    config = copy.deepcopy(base.CONFIG)
    config["backends"]["alpha"].pop("budget_check")
    config["backends"]["beta"]["budget_check"] = True
    config["backends"]["alpha"]["lane_document"] = str(lane)
    config["local_backend"] = "alpha"
    config["routing"] = {"picker": "route"}
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None)
    calls = []

    def pick(*_args, **_kwargs):
        calls.append(True)
        action = "hold" if choice == "hold" else "route"
        return SimpleNamespace(
            as_dict=lambda: {
                "action": action,
                "backend": choice if action == "route" else None,
                "confidence": 0.9,
                "probabilities": {action: 0.9},
                "excluded": [],
            }
        )

    monkeypatch.setattr(picker, "pick", pick)
    args = base._arguments(dispatch_repo, node="congestion-test", dry_run=False)
    result = CliRunner().invoke(
        cli.main, [*args, "--no-watch", *(["--local"] if choice == "explicit" else [])]
    )
    payload = json.loads(result.output.splitlines()[0])
    assert len(calls) == (0 if choice == "explicit" else 1)
    assert result.exit_code == (
        0 if choice == "beta" else 3 if choice == "hold" else 75
    ), result.output
    if choice == "beta":
        assert payload["backend"] == payload["picker_selection"]["backend"] == "beta"
    elif choice == "hold":
        assert payload["error"] == "budget-hold"
        assert payload["hold"]["picker_selection"]["action"] == "hold"
    else:
        assert payload["error"] == "lane-paused"
        assert payload["lane_gate"]["allowance"] == 0
        assert "grants 0" in payload["detail"]
