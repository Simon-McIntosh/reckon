"""No test reaches the live Jev service or reads its credential.

Since ``routing.picker`` became ``route`` by default, any dispatch that names no
lane asks Jev over OpenRouter. That made the suite depend on an external service
and on its judgement, and spend money on every run. The autouse ``no_live_jev``
fixture in ``conftest`` removes the credential and points credential resolution
at a path holding none, so the client raises ``LiveJevDisabled`` before it
builds a request.

These tests hold that boundary rather than the fixture's implementation: the
network call is never made, the recorded fallback names the disability, a routed
dispatch resolves the same backend as deterministic routing, no flight
resolution reads the repository's model catalogue, and the real credential is
unreadable while the suite runs.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.request
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew.picker import client
from tests.test_picker import config as config  # noqa: PLC0414 - re-export fixture
from tests.test_picker import (
    live_facts as live_facts,  # noqa: PLC0414 - re-export fixture
)
from tests.test_picker_in_dispatch import (
    repo as repo,  # noqa: PLC0414 - re-export fixture
)


@pytest.fixture
def routed_config(config, monkeypatch):
    """Resolve flight so a dispatch that names no lane routes through the picker."""
    config = deepcopy(config)
    config["routing"] = {"picker": "route"}
    config["fences"] = {"time_budget": "25m", "needs_help_after_failures": 2}
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config)
    return config


def invoke(repo: Path, *, route: str | None = None):
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
        "no-live-jev",
        "--goal",
        "route without a live call",
        "--done-when",
        "pytest checks the disabled picker",
        "--write-path",
        "result.json",
        "--session",
        "session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
    ]
    if route is not None:
        args += ["--route", route]
    result = CliRunner().invoke(cli.main, args)
    return result, json.loads(result.output.splitlines()[0])


def test_a_routed_dispatch_never_calls_the_live_service(
    repo, live_facts, routed_config, monkeypatch
):
    """A picker-routed dispatch reaches the disabled guard, not the network.

    The spy fails the test outright if ``urlopen`` is reached, so a clean pass
    is evidence the request was never built rather than that it merely failed.
    An eligible backend is offered, so the picker genuinely reaches its client
    step and the fallback is the disabled guard's own, not an empty candidate
    set's.
    """
    reached = []

    def spy(*args, **kwargs):
        reached.append((args, kwargs))
        raise AssertionError("urllib.request.urlopen was reached under test")

    monkeypatch.setattr(urllib.request, "urlopen", spy)

    routed_result, routed = invoke(repo, route="picker")
    assert reached == [], "the picker opened a connection to the live service"
    assert routed_result.exit_code == 0, routed_result.output

    selection = routed["picker_selection"]
    assert selection is not None
    assert selection["offered"], "no eligible backend reached the picker's client step"
    assert "LiveJevDisabled" in selection["fallback_reason"], selection[
        "fallback_reason"
    ]
    assert selection["action"] == "fallback"
    assert selection["backend"] == routed_config["default_backend"]

    deterministic_result, deterministic = invoke(repo, route="deterministic")
    assert routed_result.exit_code == deterministic_result.exit_code
    assert routed["backend"] == deterministic["backend"]


def test_a_present_credential_is_refused_before_the_network(
    repo, live_facts, routed_config, monkeypatch
):
    """A credential alone does not let a test reach the live service.

    The fixture removes the credential, which isolates the suite only while no
    test sets one back. Here a key IS set, so credential resolution succeeds and
    the client would build and send its request; the fixture's HTTP guard is then
    the only thing between the dispatch and a live call. Any attempt to look the
    service host up is caught at the socket layer, beneath that guard, so a pass
    is evidence the request was refused rather than merely failing.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY_RECKON", "fake-credential")

    reached = []

    def refuse(*args, **kwargs):
        reached.append(args)
        raise AssertionError("a network call was attempted under test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    result, payload = invoke(repo)
    assert result.exit_code == 0, result.output
    assert reached == [], "the dispatch resolved the service host"

    selection = payload["picker_selection"]
    assert selection is not None
    assert selection["action"] == "fallback"
    assert selection["fallback_reason"] == "jev-error: LiveJevDisabledError"
    assert selection["backend"] == routed_config["default_backend"]


def test_the_real_credential_is_unreadable_under_test():
    """The credential the live service would use cannot be read while tests run."""
    marker = os.environ.get(client.CREDENTIAL_ENV)
    assert marker, "the fixture did not redirect credential resolution"
    path = client.credential_path()
    assert str(path) == marker
    assert not path.is_file()
    with pytest.raises(client.LiveJevDisabledError):
        client.load_key(path)


def test_a_host_backend_is_not_filled_from_the_repository_catalogue(
    tmp_path, monkeypatch
):
    """A fixture host backend resolves with no catalogue-supplied value.

    The catalogue is repository state living in the checkout's ``docs/state``
    tree. Without isolation every flight resolution drew alias, effort, model,
    budget group and rates from it into whatever backend a fixture host
    declared, so a host naming only a model also resolved the catalogue's
    alias and effort for that backend.
    """
    from reckon import flight

    real_catalogue = (
        Path(flight.__file__).resolve().parent.parent
        / "docs"
        / "state"
        / "reckon"
        / "model-catalogue.yaml"
    )
    assert real_catalogue.is_file(), "the catalogue to hide is absent"
    assert flight.model_catalogue_path() != real_catalogue

    host = tmp_path / "host" / "flight.yaml"
    host.parent.mkdir(parents=True)
    host.write_text(
        "default_backend: clive\n"
        "backends:\n"
        "  clive:\n"
        "    launch: cli\n"
        "    command: clive\n"
        "    model: fixture-model\n"
    )

    opened: list[str] = []
    real_reader = flight.read_layer_file

    def spy(path):
        opened.append(str(Path(path).resolve()))
        return real_reader(path)

    monkeypatch.setattr(flight, "read_layer_file", spy)

    resolved = flight.resolve(
        host_path=host, project_path=tmp_path / "project" / "flight.yaml"
    )

    clive = resolved.config["backends"]["clive"]
    assert clive["model"] == "fixture-model"
    assert "alias" not in clive
    assert "effort" not in clive
    assert resolved.origin("backends.clive.alias") is None
    assert str(real_catalogue.resolve()) not in opened
