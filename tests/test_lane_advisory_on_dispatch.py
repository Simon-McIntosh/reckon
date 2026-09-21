"""A dispatch carries its lane's trajectory unasked, and refuses nothing with it."""

from __future__ import annotations

import copy
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from tests import test_dispatch_names_its_backend as existing_backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

pytest_plugins = ("tests.test_dispatch_names_its_backend",)

ROLE = "implement"
SPEC_LEVEL = "exact"


def _run(
    *,
    lane: str,
    plan: str,
    index: int,
    input_tokens: float,
    coordinator_tokens: float,
) -> dict:
    return {
        "plan": plan,
        "role": ROLE,
        "spec_level": SPEC_LEVEL,
        "backend": lane,
        "input_tokens": input_tokens,
        "changed_lines": {"added": 10, "removed": 0},
        "gate": "passed",
        "completed_at_source": "terminal_event",
        "node_definition": {
            "write_paths": [f"src/{plan}-{index}.py"],
            "coordinator": {
                "authoring_turn": {"tokens": {"input_tokens": coordinator_tokens}}
            },
        },
    }


def _cheap_lane_runs(lane: str, *, count: int = 12, cost: float = 200.0) -> list[dict]:
    """One run per plan, so no run is re-touched and the lane reads zero rework."""
    return [
        _run(
            lane=lane,
            plan=f"plan-{lane}-{index}",
            index=index,
            input_tokens=cost,
            coordinator_tokens=50.0,
        )
        for index in range(count)
    ]


def _dear_lane_runs(lane: str, *, count: int = 12, cost: float = 1000.0) -> list[dict]:
    """Every run on one plan sharing one path, so all but the last are reworked."""
    return [
        {
            **_run(
                lane=lane,
                plan=f"plan-{lane}",
                index=index,
                input_tokens=cost,
                coordinator_tokens=50.0,
            ),
            "node_definition": {
                "write_paths": ["src/shared.py"],
                "coordinator": {"authoring_turn": {"tokens": {"input_tokens": 50.0}}},
            },
        }
        for index in range(count)
    ]


def _observation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    utilisation_pct: float | None = 46.0,
    burn_multiple: float | None = 5.7,
    projected_in_seconds: int | None = 300,
    resets_seconds: int = 6 * 24 * 3600,
    calls: list[dict] | None = None,
) -> None:
    """Fix the lane at one spending trajectory, counting reads of it."""
    now = datetime.now(UTC)
    state = {
        "headroom": "known",
        "utilisation_pct": utilisation_pct,
        "burn_multiple": burn_multiple,
        "projected_exhaustion_at": (
            None
            if projected_in_seconds is None
            else (now + timedelta(seconds=projected_in_seconds)).isoformat()
        ),
        "resets_at": (now + timedelta(seconds=resets_seconds)).isoformat(),
        "seconds_until_reset": resets_seconds,
        "observed_at": now.isoformat(),
    }

    def _read(*_args, **_kwargs):
        if calls is not None:
            calls.append(state)
        return state

    monkeypatch.setattr(dispatch_module, "_dispatch_lane_observation", _read)


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    lane: str = "alpha",
    runs: list[dict] | None = None,
):
    config = copy.deepcopy(existing_backend_tests.CONFIG)
    monkeypatch.setattr(
        cli_module, "_resolved_flight", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        dispatch_module,
        "_lane_advisory_ledger_runs",
        lambda *_args, **_kwargs: list(runs or []),
    )
    result = CliRunner().invoke(
        cli_module.main,
        [*existing_backend_tests._arguments(repo, node=node), "--backend", lane],
    )
    return existing_backend_tests._payload(result), result


def test_emitted_advisory_carries_the_four_figures_and_names_a_cheaper_lane(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    runs = _dear_lane_runs("alpha") + _cheap_lane_runs("clive")
    payload, result = _invoke(dispatch_repo, monkeypatch, node="emitted", runs=runs)

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["state"] == "emitted"
    assert advisory["utilisation_pct"] == 46.0
    assert advisory["burn_multiple"] == 5.7
    assert advisory["projected_exhaustion_at"] is not None
    assert advisory["resets_at"] is not None
    assert advisory["precedes_horizon"] is True
    assert advisory["cheaper_lane"]["lane"] == "clive"
    assert advisory["cheaper_lane"]["state"] == "measured"


def test_advisory_is_emitted_unasked_and_adds_no_second_probe(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    _observation(monkeypatch, projected_in_seconds=300, calls=calls)
    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="unasked",
        runs=_dear_lane_runs("alpha") + _cheap_lane_runs("clive"),
    )

    assert result.exit_code == 0
    assert payload["lane_advisory"]["state"] == "emitted"
    # The observation is read once, as before the advisory existed: the
    # trajectory rides the reading the dispatch already takes.
    assert len(calls) == 1


def test_resolved_backend_is_identical_with_and_without_the_advisory(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _dear_lane_runs("alpha") + _cheap_lane_runs("clive")
    _observation(monkeypatch, projected_in_seconds=300)
    at_risk, first = _invoke(dispatch_repo, monkeypatch, node="at-risk", runs=runs)
    _observation(monkeypatch, projected_in_seconds=3000)
    later, second = _invoke(dispatch_repo, monkeypatch, node="after-horizon", runs=runs)
    _observation(monkeypatch, projected_in_seconds=None)
    unmeasured, third = _invoke(
        dispatch_repo, monkeypatch, node="no-projection", runs=runs
    )

    assert [first.exit_code, second.exit_code, third.exit_code] == [0, 0, 0]
    assert at_risk["lane_advisory"]["state"] == "emitted"
    assert later["lane_advisory"]["state"] == "quiet"
    assert unmeasured["lane_advisory"]["state"] == "quiet"
    assert at_risk["backend"] == later["backend"] == unmeasured["backend"] == "alpha"
    assert (
        at_risk["agent"]["backend"]
        == later["agent"]["backend"]
        == unmeasured["agent"]["backend"]
        == "alpha"
    )


def test_a_projection_after_the_horizon_says_so_rather_than_going_silent(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=3000)
    payload, result = _invoke(dispatch_repo, monkeypatch, node="after-horizon")

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["state"] == "quiet"
    assert advisory["precedes_horizon"] is False
    assert "after this node's" in advisory["detail"]
    assert advisory["cheaper_lane"]["state"] == "not_evaluated"


def test_an_unmetered_lane_states_it_has_no_window_to_exhaust(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="local-lane", lane="clive"
    )

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["metered"] is False
    assert advisory["state"] == "quiet"
    assert "unmetered" in advisory["detail"]


def test_an_absent_ledger_names_no_lane_and_does_not_refuse(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    payload, result = _invoke(dispatch_repo, monkeypatch, node="no-ledger", runs=[])

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["state"] == "emitted"
    assert advisory["cheaper_lane"]["lane"] is None
    assert advisory["cheaper_lane"]["state"] == "insufficient_evidence"


def test_too_few_runs_states_the_shortfall_instead_of_naming_a_lane() -> None:
    runs = [
        _run(
            lane="alpha",
            plan="p",
            index=0,
            input_tokens=1000.0,
            coordinator_tokens=50.0,
        ),
        _run(
            lane="clive", plan="q", index=1, input_tokens=10.0, coordinator_tokens=10.0
        ),
    ]
    clause = dispatch_module._lane_advisory_cheaper_lane(
        runs,
        resolved_lane="alpha",
        role=ROLE,
        spec_level=SPEC_LEVEL,
        configured_lanes=["alpha", "clive"],
    )
    assert clause["lane"] is None
    assert clause["state"] == "insufficient_evidence"
    assert "1 usable run(s)" in clause["detail"]
    assert "10 needed" in clause["detail"]


def test_no_lane_cheaper_is_stated_when_the_resolved_lane_already_wins() -> None:
    runs = _cheap_lane_runs("alpha") + _dear_lane_runs("clive")
    clause = dispatch_module._lane_advisory_cheaper_lane(
        runs,
        resolved_lane="alpha",
        role=ROLE,
        spec_level=SPEC_LEVEL,
        configured_lanes=["alpha", "clive"],
    )
    assert clause["lane"] is None
    assert clause["state"] == "none_cheaper"
    assert "no configured lane" in clause["detail"]


def test_a_cheaper_lane_is_named_from_its_rework_charged_cost() -> None:
    runs = _dear_lane_runs("alpha") + _cheap_lane_runs("clive")
    clause = dispatch_module._lane_advisory_cheaper_lane(
        runs,
        resolved_lane="alpha",
        role=ROLE,
        spec_level=SPEC_LEVEL,
        configured_lanes=["alpha", "clive"],
    )
    assert clause["state"] == "measured"
    assert clause["lane"] == "clive"
    assert clause["cost_per_durable_node"] < clause["resolved_cost_per_durable_node"]
    assert clause["samples"] == 12
