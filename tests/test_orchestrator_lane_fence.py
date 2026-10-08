"""A lane declaring the orchestrator role stops a dispatch to it, by declaration.

One subscription runs every orchestrator on this workstation, so background
work placed on the same lane spends the capacity the sessions that dispatch,
merge, promote and record need. The role is a property of the deployment, so it
is declared in flight config rather than matched against a backend's name: a
declaration survives an alias and survives an orchestrator that moves, and a
lane that declares nothing is dispatchable exactly as before.

Every case runs against the stub fleet another dispatch test already builds, in
a temporary ``RECKON_HOME``; the declaration is the only new variable.
"""

from __future__ import annotations

import importlib
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_plan as dispatch_plan_module
from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands

# The CLI cases reuse the stub fleet and git repository the backend-routing
# dispatch test builds, so the declaration is the only new variable.
pytest_plugins = ("tests.test_dispatch_names_its_backend",)

from tests import test_dispatch_names_its_backend as backend_tests  # noqa: E402

dispatch_module = importlib.import_module("reckon.crew.dispatch")

# A stub backend that carries no orchestrator until a case declares it.
DECLARING_LANE = "clive"


def _config(*, declaring: str = DECLARING_LANE) -> dict:
    """Declare one lane as serving orchestrators, leaving its siblings alone."""
    config = deepcopy(backend_tests.CONFIG)
    config["backends"][declaring]["serves_orchestrators"] = True
    return config


def _cli(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    config: dict,
    extra=None,
    dry_run: bool = True,
):
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            *backend_tests._arguments(repo, node=node, dry_run=dry_run),
            *(extra or []),
        ],
    )
    return backend_tests._payload(result), result


def _fence_warning_lines(payload: dict) -> list[str]:
    return [
        line
        for line in (payload.get("warnings") or [])
        if "serves_orchestrators" in str(line)
    ]


# ── the declaration is read ─────────────────────────────────────────────────


def test_a_declaring_lane_stops_the_dispatch_and_names_lane_reason_discharge(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane, why it is fenced and a way off it all reach the caller."""
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="declared-lane",
        config=_config(),
        extra=["--backend", DECLARING_LANE],
    )

    stop = payload["orchestrator_lane_stop"]
    assert result.exit_code == 0, result.output
    assert stop["state"] == "declared"
    assert stop["lane"] == DECLARING_LANE
    assert "orchestrators" in stop["detail"]
    assert "serves_orchestrators" in stop["detail"]
    assert stop["discharge"].strip()
    assert DECLARING_LANE not in stop["discharge"]
    assert _fence_warning_lines(payload), payload["warnings"]


def test_the_discharge_names_a_configured_lane_that_serves_no_orchestrator(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal with no way through is a stop, so the discharge is a lane."""
    config = _config()
    payload, _result = _cli(
        dispatch_repo,
        monkeypatch,
        node="discharge-lane",
        config=config,
        extra=["--backend", DECLARING_LANE],
    )

    discharge = payload["orchestrator_lane_stop"]["discharge"]
    served = [
        name
        for name in config["backends"]
        if name != DECLARING_LANE and repr(name) in discharge
    ]
    assert served, discharge
    assert all(
        not config["backends"][name].get("serves_orchestrators") for name in served
    )


def test_the_declaration_is_carried_and_absent_by_default() -> None:
    """The schema holds the declaration, and a lane that omits it declares nothing."""
    from reckon._flight_schema import BackendConfig

    assert BackendConfig(
        name="lane", launch="cli", serves_orchestrators=True
    ).serves_orchestrators
    assert BackendConfig(name="lane", launch="cli").serves_orchestrators is None


# ── the declaration decides, not the lane ───────────────────────────────────


def test_removing_the_declaration_stops_the_fence_firing(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same node, same lane: only the declaration is removed."""
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="declared-lane",
        config=backend_tests.CONFIG,
        extra=["--backend", DECLARING_LANE],
    )

    assert result.exit_code == 0
    assert payload["backend"] == DECLARING_LANE
    assert payload["orchestrator_lane_stop"]["state"] == "not-declared"
    assert _fence_warning_lines(payload) == []


def test_a_lane_declaring_nothing_launches_unstopped(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same node on a lane that declares nothing launches."""
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="undeclared-lane",
        config=_config(),
        extra=["--backend", "beta", "--no-watch"],
        dry_run=False,
    )

    assert result.exit_code == 0
    assert payload["backend"] == "beta"
    assert payload["orchestrator_lane_stop"]["state"] == "not-declared"
    assert _fence_warning_lines(payload) == []


# ── the stop is recorded, and it lands before the writes ────────────────────


def test_the_stop_is_recorded_on_the_run_that_launched(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane is recorded rather than refused, so the run carries the cost."""
    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="recorded-lane",
        config=_config(),
        extra=["--backend", DECLARING_LANE, "--no-watch"],
        dry_run=False,
    )

    assert result.exit_code == 0
    assert payload["orchestrator_lane_stop"]["state"] == "declared"
    row = next(
        row for row in crew.list_live() if row.get("run_id") == payload["run_id"]
    )
    assert row["orchestrator_lane_stop"]["state"] == "declared"
    assert row["orchestrator_lane_stop"]["severity"] == "recorded"
    assert row["orchestrator_lane_stop"]["discharge"].strip()
    assert _fence_warning_lines(dict(row)), row["warnings"]


def test_nothing_is_written_before_the_stop(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declaration is read before a pointer, a run directory or a worktree.

    The stop is composed in resolution, so the order of the two probes is the
    whole claim: at the moment the fence reads the declaration no live pointer
    exists, and the first durable write of the dispatch follows it.
    """
    order: list[str] = []
    pointers_at_stop: list[int] = []
    real_stop = dispatch_module._dispatch_orchestrator_lane_stop
    real_claim = dispatch_module._publish_launch_claim
    real_worktree = dispatch_module._create_worktree

    def spy_stop(**kwargs):
        pointers_at_stop.append(len(crew.list_live()))
        order.append("stop")
        return real_stop(**kwargs)

    def spy_claim(*args, **kwargs):
        order.append("claim")
        return real_claim(*args, **kwargs)

    def spy_worktree(*args, **kwargs):
        order.append("worktree")
        return real_worktree(*args, **kwargs)

    monkeypatch.setattr(dispatch_plan_module, "_dispatch_orchestrator_lane_stop", spy_stop)
    monkeypatch.setattr(dispatch_module, "_publish_launch_claim", spy_claim)
    monkeypatch.setattr(dispatch_module, "_create_worktree", spy_worktree)

    payload, result = _cli(
        dispatch_repo,
        monkeypatch,
        node="fenced-order",
        config=_config(),
        extra=["--backend", DECLARING_LANE, "--no-watch"],
        dry_run=False,
    )

    assert result.exit_code == 0
    assert payload["orchestrator_lane_stop"]["state"] == "declared"
    assert pointers_at_stop == [0]
    assert order[0] == "stop"
    assert "claim" in order and order.index("stop") < order.index("claim")
    assert "worktree" in order and order.index("stop") < order.index("worktree")
