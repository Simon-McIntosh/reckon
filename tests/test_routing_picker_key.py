"""Flight routing is layered and a dispatch can record its switch back."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from reckon import _store, cli, crew, flight
from reckon.crew import picker
from reckon.crew.dispatch import change_lane
from reckon.crew.dispatch import shadow as dispatch_shadow
from tests.test_picker_in_dispatch import CONFIG, selection
from tests.test_picker_in_dispatch import (
    repo as repo,  # noqa: PLC0414 - re-export the pytest fixture
)


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
    assert payload["picker_selection"]["backend"] == "beta"
    assert payload["route"] == expected_route
    assert payload["route_override"] == override
    if not dry_run:
        home = _store._config_home()
        record = json.loads(
            (home / "crew" / "live" / f"{payload['run_id']}.json").read_text()
        )
        assert record["route"] == expected_route
        assert record["route_override"] == override


def test_shipped_routing_default_is_shadow(tmp_path):
    resolved = flight.resolve(host_path=tmp_path / "absent.yaml")
    assert resolved.config["routing"]["picker"] == "shadow"
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
