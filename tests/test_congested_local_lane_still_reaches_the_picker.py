"""A busy local lane is judged after a dispatch's routing choice."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import picker
from tests.test_picker_in_dispatch import CONFIG

pytest_plugins = ("tests.test_picker_in_dispatch",)


@pytest.fixture
def congested_config(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    lane = repo.parent / "lane.json"
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
        ),
        encoding="utf-8",
    )
    config = copy.deepcopy(CONFIG)
    config["local_backend"] = "alpha"
    config["routing"] = {"picker": "route"}
    config["backends"]["alpha"]["lane_document"] = str(lane)
    monkeypatch.setattr(cli, "_resolved_flight", lambda *_a, **_k: config)
    return config


def _invoke(repo: Path, *extra: str):
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            "proj",
            "--plan",
            "example",
            "--section",
            "dispatch",
            "--role",
            "implement",
            "--spec-level",
            "exact",
            "--node",
            "congestion-test",
            "--goal",
            "record the chosen backend",
            "--done-when",
            "pytest checks the selected backend",
            "--write-path",
            "result.json",
            "--session",
            "session",
            "--repo",
            str(repo),
            "--no-watch",
            *extra,
        ],
    )
    return result, json.loads(result.output.splitlines()[0])


def _selection(action: str, backend: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        as_dict=lambda: {
            "action": action,
            "backend": backend,
            "confidence": 0.9,
            "probabilities": {action: 0.9},
            "excluded": [],
        }
    )


def test_unnamed_lane_can_launch_on_metered_backend(
    repo: Path, congested_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def pick(*_args, **_kwargs):
        calls.append(True)
        return _selection("route", "beta")

    monkeypatch.setattr(picker, "pick", pick)
    result, payload = _invoke(repo)
    assert result.exit_code == 0, result.output
    assert calls == [True]
    assert payload["backend"] == "beta"
    assert payload["picker_selection"]["backend"] == "beta"


def test_unnamed_lane_refuses_when_picker_chooses_busy_local_backend(
    repo: Path, congested_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def pick(*_args, **_kwargs):
        calls.append(True)
        return _selection("route", "alpha")

    monkeypatch.setattr(picker, "pick", pick)
    result, payload = _invoke(repo)
    assert result.exit_code == 75, result.output
    assert calls == [True]
    assert payload["error"] == "lane-paused"
    assert payload["lane_gate"]["allowance"] == 0
    assert "grants 0" in payload["detail"]


def test_unnamed_lane_records_picker_hold(
    repo: Path, congested_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: _selection("hold"))
    result, payload = _invoke(repo)
    assert result.exit_code == 3, result.output
    assert payload["error"] == "budget-hold"
    assert payload["hold"]["picker_selection"]["action"] == "hold"


def test_named_local_lane_refuses_before_picker(
    repo: Path, congested_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: calls.append(True))
    result, payload = _invoke(repo, "--local")
    assert result.exit_code == 75, result.output
    assert calls == []
    assert payload["error"] == "lane-paused"
    assert "grants 0" in payload["detail"]
