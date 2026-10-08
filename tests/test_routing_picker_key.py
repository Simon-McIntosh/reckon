"""Flight routing is layered and a dispatch can record its switch back."""

from __future__ import annotations

import importlib
import json
import time
from copy import deepcopy

import pytest
from click.testing import CliRunner

from reckon import _store, cli, crew, flight
from reckon.crew import dispatch_plan as dispatch_plan_module
from reckon.crew import picker
from reckon.crew.dispatch import change_lane
from reckon.crew.dispatch import shadow as dispatch_shadow
from tests.test_picker_in_dispatch import CONFIG, selection
from tests.test_picker_in_dispatch import (
    repo as repo,  # noqa: PLC0414 - re-export the pytest fixture
)

dispatch_module = importlib.import_module("reckon.crew.dispatch")


def invoke(repo, *, route=None, dry_run=False):
    args = [
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
        "routing-test",
        "--goal",
        "record routing",
        "--done-when",
        "pytest checks routing",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--repo",
        str(repo),
        "--no-watch",
    ]
    if route is not None:
        args += ["--route", route]
    if dry_run:
        args += ["--dry-run"]
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output.splitlines()[0])


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("configured", "override", "expected_backend", "expected_route"),
    [
        ("shadow", None, "alpha", "shadow"),
        ("route", None, "beta", "picker"),
        ("route", "deterministic", "alpha", "deterministic"),
        ("shadow", "picker", "beta", "picker"),
    ],
)
def test_dispatch_resolves_routing_key(
    repo, monkeypatch, dry_run, configured, override, expected_backend, expected_route
):
    config = deepcopy(CONFIG)
    config["routing"] = {"picker": configured}
    monkeypatch.setattr(cli, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: selection())
    payload = invoke(repo, route=override, dry_run=dry_run)
    assert payload["backend"] == expected_backend
    if expected_route != "picker" and (dry_run or expected_route == "deterministic"):
        # A preview carries the picker answer only when it routed by one; a
        # shadow or deterministic preview reads the same whatever the picker
        # said. A deterministic launch records its shadow answer after return.
        assert payload["picker_selection"] is None
    else:
        assert payload["picker_selection"]["backend"] == "beta"
    assert payload["route"] == expected_route
    assert payload["route_override"] == override
    if not dry_run:
        home = _store._config_home()
        pointer = home / "crew" / "live" / f"{payload['run_id']}.json"
        record = json.loads(pointer.read_text())
        assert record["route"] == expected_route
        assert record["route_override"] == override
        assert record["backend"] == expected_backend
        if expected_route == "deterministic":
            deadline = (
                time.monotonic() + dispatch_module.PICKER_DISPATCH_TIMEOUT_SECONDS + 5
            )
            while record["picker_selection"] is None and time.monotonic() < deadline:
                time.sleep(0.05)
                record = json.loads(pointer.read_text())
            assert record["picker_selection"] is not None
            assert record["picker_selection"]["action"] in {
                "route",
                "refuse",
                "fallback",
                "hold",
            }


def test_absent_picker_selection_resolves_deterministically():
    """A routed caller with no picker answer falls back rather than refusing.

    ``plan_dispatch`` is reached without a selection by any caller that does
    not run dispatch's own picker step — a validating dry run, or an internal
    re-dispatch. It must resolve the deterministic backend and record why the
    picker's answer is absent, instead of raising for a missing selection.
    """
    config = deepcopy(CONFIG)
    config["routing"] = {"picker": "route"}
    resolution = crew.plan_dispatch(
        node=crew.TaskNode(
            id="absent-selection",
            goal="resolve a routed dispatch with no picker answer",
            plan="example",
            section="dispatch",
            spec_level="exact",
            done_when="pytest checks the deterministic fallback",
            write_paths=["result.json"],
        ),
        config=config,
    )
    assert resolution.route == "picker"
    assert resolution.backend == "alpha"
    assert resolution.picker_selection["action"] == "fallback"
    assert resolution.picker_selection["fallback_reason"] == "picker-selection-absent"


def _without_clock_stamps(value):
    """Mask observation stamps so two previews compare on their decisions alone."""
    if isinstance(value, dict):
        return {
            key: ("<clock>" if key.endswith("_at") else _without_clock_stamps(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_clock_stamps(item) for item in value]
    return value


@pytest.mark.parametrize("mode", ["shadow", "route"])
def test_a_dry_run_report_is_comparable_across_picker_modes(repo, monkeypatch, mode):
    """Two previews of one node differ in no field the picker's timing touches.

    The picker is stubbed to report a different latency on each ask, so a report
    that kept the per-call figure would read as two different dispatches. A
    shadow preview must not carry the picker's answer at all; a routed preview
    keeps the decision it routed by and drops the timing behind it.
    """
    config = deepcopy(CONFIG)
    config["routing"] = {"picker": mode}
    monkeypatch.setattr(cli, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(dispatch_plan_module, "new_run_id", lambda _node: "r-fixed")
    latencies = iter([1.0, 999.0])

    def pick(*_a, **_k):
        return selection(latency_ms=next(latencies))

    monkeypatch.setattr(picker, "pick", pick)
    first = invoke(repo, dry_run=True)
    second = invoke(repo, dry_run=True)

    assert _without_clock_stamps(first) == _without_clock_stamps(second)
    if mode == "shadow":
        assert first["picker_selection"] is None
    else:
        assert first["picker_selection"]["action"] == "route"
        assert first["picker_selection"]["backend"] == "beta"
        assert "latency_ms" not in first["picker_selection"]
        assert "jev_latency_ms" not in first["picker_selection"]


def test_shipped_routing_default_is_route(tmp_path):
    """Route is the shipped picker default, asserted through a shipped layer.

    The shipped file moves to ``route`` with the routing-default change, so the
    assertion is applied against a configuration fixture that declares route
    and reads back as the shipped layer, holding on either side of that change.
    """
    shipped = tmp_path / "flight-defaults.yaml"
    shipped.write_text("routing:\n  picker: route\n")
    resolved = flight.resolve(
        host_path=tmp_path / "absent.yaml",
        shipped_path=shipped,
    )
    assert resolved.config["routing"]["picker"] == "route"
    assert resolved.provenance["routing.picker"] == "shipped"


def test_project_routing_layer_drives_dispatch(repo, monkeypatch, tmp_path):
    project_layer = tmp_path / "flight.yaml"
    project_layer.write_text("routing:\n  picker: route\n")
    resolved = flight.resolve(
        host_path=tmp_path / "absent.yaml",
        project_path=project_layer,
        overrides=CONFIG,
    )
    assert resolved.provenance["routing.picker"] == "project"
    monkeypatch.setattr(cli, "_resolved_flight", lambda *_a, **_k: resolved.config)
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: selection())
    assert invoke(repo)["backend"] == "beta"


@pytest.mark.parametrize("value", ["shadow", "route"])
def test_schema_accepts_picker_modes(value):
    flight.validate_layer({"routing": {"picker": value}}, "flight.yaml")


def test_schema_refuses_unknown_picker_mode():
    with pytest.raises(flight.FlightConfigError, match=r"routing\.picker"):
        flight.validate_layer({"routing": {"picker": "automatic"}}, "flight.yaml")


def _named_backend_node(manifest_path):
    return crew.TaskNode(
        id="named-backend",
        goal="resolve a named backend without asking the picker",
        plan="example",
        section="dispatch",
        spec_level="exact",
        done_when="pytest checks the named backend",
        write_paths=["result.json"],
        time_budget="25m",
        manifest_path=str(manifest_path),
    )


def _dispatch_primary(repo, config, monkeypatch):
    """Run one node so a completed ledger record exists to shadow or move."""
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: selection())
    return crew.dispatch(
        node=_named_backend_node(repo.parent / "named-backend-manifest.md"),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=lambda *_a, **_k: 0,
        backend_override="alpha",
    )


def test_shadow_naming_a_backend_stays_deterministic_under_route(
    repo, monkeypatch
):
    """A shadow names its candidate, so the picker route cannot ask it for a selection."""
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: selection())
    primary = _dispatch_primary(repo, deepcopy(CONFIG), monkeypatch)
    crew.complete(primary["run_id"], gate="passed")
    route_config = deepcopy(CONFIG)
    route_config["routing"] = {"picker": "route"}

    record = dispatch_shadow(
        str(primary["run_id"]),
        candidate_backend="alpha",
        config=route_config,
        repo=repo,
        session="shadow-session",
        launcher=lambda *_a, **_k: 0,
    )

    assert record["backend"] == "alpha"
    assert record["route"] == "deterministic"


def test_lane_change_naming_a_backend_stays_deterministic_under_route(
    repo, monkeypatch
):
    """A lane change names its destination, so the picker route cannot ask it for a selection."""
    primary = _dispatch_primary(repo, deepcopy(CONFIG), monkeypatch)
    route_config = deepcopy(CONFIG)
    route_config["routing"] = {"picker": "route"}

    moved = change_lane(
        str(primary["run_id"]),
        "beta",
        "the named lane is spent",
        config=route_config,
        launch=True,
    )

    assert moved["backend"] == "beta"
    assert moved["route"] == "deterministic"
