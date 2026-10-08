"""An explicitly named lane implies deterministic routing under the route default.

Once ``routing.picker: route`` is the shipped default, a dispatch that names a
backend on the command line must still run on that lane: the named lane is
itself a routing instruction, so the picker has nothing to select. Only an
explicit ``--route picker`` beside a named lane contradicts itself and is
refused. A dispatch that names no lane keeps routing as flight resolves it.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from reckon import _store, cli, crew_dispatch_commands
from reckon.crew import picker
from tests.test_picker_in_dispatch import CONFIG, selection
from tests.test_picker_in_dispatch import (
    repo as repo,  # noqa: PLC0414 - re-export the pytest fixture
)

_BASE_ARGS = [
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
    "explicit-lane-test",
    "--goal",
    "name a lane",
    "--done-when",
    "pytest checks the named lane",
    "--write-path",
    "result.json",
    "--session",
    "session",
]


def _invoke(repo, *, extra=(), route=None, dry_run=True):
    args = [*_BASE_ARGS, "--repo", str(repo), "--no-watch"]
    if route is not None:
        args += ["--route", route]
    args += list(extra)
    if dry_run:
        args += ["--dry-run"]
    return CliRunner().invoke(cli.main, args)


@pytest.fixture
def route_config(repo, monkeypatch):
    """Resolve flight to the shipped route default and forbid a picker call."""
    config = deepcopy(CONFIG)
    config["routing"] = {"picker": "route"}
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(crew_dispatch_commands, "_layer_flight_config", lambda *_a, **_k: config)
    return config


def _forbid_picker(monkeypatch):
    """Record every picker ask, monkeypatched onto the client entry point."""
    calls: list[object] = []

    def pick(*args, **kwargs):
        calls.append((args, kwargs))
        return selection()

    monkeypatch.setattr(picker, "pick", pick)
    return calls


def test_backend_without_route_implies_deterministic(repo, route_config, monkeypatch):
    """Rule 1: ``--backend`` with no ``--route`` runs on that lane, no picker ask."""
    calls = _forbid_picker(monkeypatch)
    result = _invoke(repo, extra=["--backend", "alpha"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "alpha"
    assert payload["route"] == "deterministic"
    assert payload["picker_selection"] is None
    assert calls == [], "the picker client was asked for an explicit lane"


def test_local_without_route_implies_deterministic(repo, route_config, monkeypatch):
    """Rule 1: ``--local`` with no ``--route`` runs on the local lane, no ask."""
    route_config["local_backend"] = "alpha"
    calls = _forbid_picker(monkeypatch)
    result = _invoke(repo, extra=["--local"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "alpha"
    assert payload["route"] == "deterministic"
    assert payload["picker_selection"] is None
    assert calls == [], "the picker client was asked for an explicit lane"


def test_set_default_backend_without_route_implies_deterministic(
    repo, route_config, monkeypatch
):
    """Rule 1: ``--set default_backend`` names a lane, so no picker ask."""
    calls = _forbid_picker(monkeypatch)
    result = _invoke(repo, extra=["--set", "default_backend=alpha"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "alpha"
    assert payload["route"] == "deterministic"
    assert payload["picker_selection"] is None
    assert calls == [], "the picker client was asked for an explicit lane"


def test_picker_route_beside_a_backend_is_refused(repo, route_config, monkeypatch):
    """Rule 2: ``--route picker`` with a named lane contradicts itself."""
    _forbid_picker(monkeypatch)
    result = _invoke(repo, extra=["--backend", "alpha"], route="picker")
    assert result.exit_code != 0
    assert "picker routing cannot be combined with --backend or --local" in (
        result.output + (result.stderr or "")
    )


def test_picker_route_beside_local_is_refused(repo, route_config, monkeypatch):
    """Rule 2 also covers ``--local``: the lane names the backend, picker cannot."""
    route_config["local_backend"] = "alpha"
    _forbid_picker(monkeypatch)
    result = _invoke(repo, extra=["--local"], route="picker")
    assert result.exit_code != 0
    assert "picker routing cannot be combined with --backend or --local" in (
        result.output + (result.stderr or "")
    )


def test_no_lane_no_route_still_routes_by_picker(repo, route_config, monkeypatch):
    """Rule 3: with no lane named, routing stays as flight resolves it."""
    calls = _forbid_picker(monkeypatch)
    result = _invoke(repo)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "beta"
    assert payload["route"] == "picker"
    assert payload["picker_selection"]["backend"] == "beta"
    assert len(calls) == 1, "the picker should be asked when no lane is named"


def test_explicit_lane_real_dispatch_records_deterministic(
    repo, route_config, monkeypatch
):
    """Rule 1 end to end: the launched run carries the named lane and its route.

    The routing decision the CLI makes is deterministic, so the named lane
    stands and the run records that lane and its route. The top-level shadow
    ``picker_selection`` a run pointer carries is written by the shared
    dispatch entry for every dispatch and is not this surface's to suppress.
    """
    _forbid_picker(monkeypatch)
    result = _invoke(repo, extra=["--backend", "alpha"], dry_run=False)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "alpha"
    assert payload["route"] == "deterministic"
    home = _store._config_home()
    record = json.loads(
        (home / "crew" / "live" / f"{payload['run_id']}.json").read_text()
    )
    assert record["backend"] == "alpha"
    assert record["route"] == "deterministic"
