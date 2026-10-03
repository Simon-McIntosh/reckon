"""No test reaches the live Jev service or reads its credential.

Since ``routing.picker`` became ``route`` by default, any dispatch that names no
lane asks Jev over OpenRouter. That made the suite depend on an external service
and on its judgement, and spend money on every run. The autouse ``no_live_jev``
fixture in ``conftest`` removes the credential and points credential resolution
at a path holding none, so the client raises ``LiveJevDisabled`` before it
builds a request.

These tests hold that boundary rather than the fixture's implementation: the
network call is never made, the recorded fallback names the disability, a routed
dispatch resolves the same backend as deterministic routing, and the real
credential is unreadable while the suite runs.
"""

from __future__ import annotations

import json
import os
import urllib.request
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
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
    monkeypatch.setattr(cli, "_resolved_flight", lambda *_a, **_k: config)
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


def test_the_real_credential_is_unreadable_under_test():
    """The credential the live service would use cannot be read while tests run."""
    marker = os.environ.get(client.CREDENTIAL_ENV)
    assert marker, "the fixture did not redirect credential resolution"
    path = client.credential_path()
    assert str(path) == marker
    assert not path.is_file()
    with pytest.raises(client.LiveJevDisabledError):
        client.load_key(path)
