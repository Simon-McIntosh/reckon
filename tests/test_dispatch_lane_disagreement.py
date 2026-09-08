"""One routing disagreement has one refusal, whichever surface expressed it."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import ledger
from tests import test_dispatch_names_its_backend as existing_backend_tests

pytest_plugins = ("tests.test_dispatch_names_its_backend",)


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    extra: list[str] | None = None,
):
    config = deepcopy(existing_backend_tests.CONFIG)

    def resolve_flight(_module, _project, _checkout_path, overrides):
        for override in overrides:
            key, value = override.split("=", 1)
            if key == "default_backend":
                config[key] = value
        return config

    monkeypatch.setattr(cli_module, "_resolved_flight", resolve_flight)
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_args, **_kwargs: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            *existing_backend_tests._arguments(repo, node=node),
            *(extra or []),
        ],
    )
    return existing_backend_tests._payload(result), result


def _register_member_without_harness(repo: Path) -> None:
    data, version = ledger.load("proj", repo)
    data["members"].append(
        {
            "id": "worker",
            "harness": "",
            "role": "implement",
            "session_id": None,
            "session_model": None,
            "sessions": {},
            "created": "2026-01-01",
        }
    )
    ledger.write("proj", data, version, repo)


def test_flight_override_and_named_backend_share_the_disagreement_refusal(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="beta", root=dispatch_repo)
    override_payload, override_result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="override-disagreement",
        extra=["--member", "worker", "--set", "default_backend=gamma"],
    )
    named_payload, named_result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="named-disagreement",
        extra=["--member", "worker", "--backend", "gamma"],
    )

    assert override_result.exit_code == named_result.exit_code == 1
    assert override_payload["error"] == named_payload["error"] == "dispatch-refused"
    assert override_payload["detail"] == named_payload["detail"]
    assert "beta" in override_payload["detail"]
    assert "gamma" in override_payload["detail"]
    assert "agent" not in override_payload


def test_matching_flight_override_and_member_harness_resolve_that_backend(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="beta", root=dispatch_repo)
    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="matching-override",
        extra=["--member", "worker", "--set", "default_backend=beta"],
    )

    assert result.exit_code == 0
    assert payload["requested_backend"] == "beta"
    assert payload["backend"] == "beta"
    assert payload["agent"]["backend"] == "beta"


def test_flight_override_routes_when_member_declares_no_harness(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_member_without_harness(dispatch_repo)
    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="override-without-member-harness",
        extra=["--member", "worker", "--set", "default_backend=gamma"],
    )

    assert result.exit_code == 0
    assert payload["requested_backend"] == "gamma"
    assert payload["backend"] == "gamma"
    assert payload["agent"]["backend"] == "gamma"


def test_member_harness_without_override_meets_lane_declaration_refusal(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.register_member("proj", "worker", harness="beta", root=dispatch_repo)
    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="member-harness-only",
        extra=["--member", "worker"],
    )

    assert result.exit_code == 2
    assert payload["validation"]["ok"] is False
    detail = payload["validation"]["findings"][0]["detail"]
    assert "beta" in detail
    assert "not known to be unmetered" in detail
    assert "is metered" not in detail
    assert payload["agent"]["backend"] == "beta"
    assert payload["lane_declaration"]["backend"] is None
    assert payload["lane_declaration"]["resolved_backend"] == "beta"
