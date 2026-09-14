"""Dispatch refuses an unserved model; flight reports whether a lane can serve."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon.flight import flight_report


@pytest.fixture()
def unavailable_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    wrapper = tmp_path / "synthetic-worker"
    wrapper.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'served-alpha gpu-a' 'served-beta gpu-b'\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    config = {
        "default_backend": "local",
        "backends": {
            "local": {
                "launch": "cli",
                "command": "synthetic-worker",
                "model": "unserved-gamma",
                "catalog": {
                    "list_command": ["synthetic-worker", "--list"],
                    "model_pattern": r"^{model}\b",
                },
                "sandbox": "worktree-full",
                "time_budget": "10m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "10m"},
    }
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: config)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return config


def _arguments(tmp_path: Path, *, dry_run: bool) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "sample",
        "--plan",
        "model-routing",
        "--section",
        "catalog",
        "--node",
        "catalog-refusal",
        "--goal",
        "Verify model-aware backend selection",
        "--done-when",
        "pytest reports zero failures",
        "--write-path",
        "package/target.py",
        "--session",
        "availability-test",
        "--repo",
        str(tmp_path),
    ]
    if dry_run:
        arguments.append("--dry-run")
    return arguments


@pytest.mark.parametrize("dry_run", [False, True])
def test_unserved_model_is_a_typed_capability_refusal(
    tmp_path: Path, unavailable_config: dict, dry_run: bool
) -> None:
    result = CliRunner().invoke(cli_module.main, _arguments(tmp_path, dry_run=dry_run))

    payload = json.loads(result.output)
    assert result.exit_code == 5
    assert payload["error"] == "competence-refusal"
    assert payload["competence"] == {
        "allowed": False,
        "backend": "local",
        "model": "unserved-gamma",
        "reason": (
            "model 'unserved-gamma' is not served; catalog offered: "
            "served-alpha gpu-a | served-beta gpu-b"
        ),
        "refusal": "model-unavailable",
    }
    assert crew.list_live() == []


# ── Serving state: whether the lane can serve, not only that it exists ───────


def _serving_report(tmp_path: Path, backend: dict) -> dict:
    """Resolve a synthetic config and return one backend's availability entry.

    The host and project layers are pointed away from the live ones, so the
    probe reads only what the caller synthesised under ``tmp_path`` and touches
    neither the live endpoints document nor the cluster.
    """
    report = flight_report(
        None,
        overrides={"default_backend": "local", "backends": {"local": backend}},
        host_path=tmp_path / "absent-host-flight.yaml",
        project_path=tmp_path / "absent-project-flight.yaml",
    )
    return report["availability"]["local"]


def _endpoints_document(tmp_path: Path, endpoints: list) -> str:
    path = tmp_path / "endpoints.json"
    path.write_text(json.dumps({"endpoints": endpoints}), encoding="utf-8")
    return str(path)


def test_serving_when_a_listed_endpoint_serves_the_configured_model(
    tmp_path: Path,
) -> None:
    document = _endpoints_document(tmp_path, [{"model_id": "synthetic-model"}])

    entry = _serving_report(
        tmp_path,
        {
            "launch": "cli",
            "command": "sh",
            "model": "synthetic-model",
            "endpoints_document": document,
        },
    )

    assert entry["serving"] == "serving"
    assert "synthetic-model" in entry["serving_detail"]


def test_mismatch_when_no_listed_endpoint_serves_the_configured_model(
    tmp_path: Path,
) -> None:
    document = _endpoints_document(
        tmp_path,
        [{"model_id": "served-alpha"}, {"model_id": "served-beta"}],
    )

    entry = _serving_report(
        tmp_path,
        {
            "launch": "cli",
            "command": "sh",
            "model": "wanted-gamma",
            "endpoints_document": document,
        },
    )

    assert entry["serving"] == "mismatch"
    # The lane is up: the document lists endpoints, so this is not the lane
    # being down, and the detail names what the lane is actually offering.
    assert "2 endpoint(s)" in entry["serving_detail"]
    assert "wanted-gamma" in entry["serving_detail"]
    assert "served-alpha, served-beta" in entry["serving_detail"]


def test_mismatch_when_the_configured_model_is_not_a_listed_one(
    tmp_path: Path,
) -> None:
    """Reproduce the measured wrong-checkpoint state: the document serves one
    model, the backend declares another, and the verdict must not read serving.
    """
    document = _endpoints_document(tmp_path, [{"model_id": "deepseek-v4-flash"}])

    entry = _serving_report(
        tmp_path,
        {
            "launch": "cli",
            "command": "sh",
            "model": "deepseek-v4.1-flash",
            "endpoints_document": document,
        },
    )

    assert entry["serving"] == "mismatch"
    assert "deepseek-v4-flash" in entry["serving_detail"]


def test_not_serving_when_the_document_lists_no_endpoint(tmp_path: Path) -> None:
    document = _endpoints_document(tmp_path, [])

    entry = _serving_report(
        tmp_path,
        {"launch": "cli", "command": "sh", "endpoints_document": document},
    )

    assert entry["serving"] == "not-serving"
    # command_found keeps its launcher-presence meaning: the wrapper exists, so
    # only the serving field carries the lane being down.
    assert entry["command_found"] is True


def test_unknown_when_no_document_is_declared(tmp_path: Path) -> None:
    entry = _serving_report(
        tmp_path,
        {"launch": "cli", "command": "sh"},
    )

    assert entry["serving"] == "unknown"
    assert entry["command_found"] is True


def test_unknown_when_the_document_is_unreadable(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"

    entry = _serving_report(
        tmp_path,
        {
            "launch": "cli",
            "command": "synthetic-worker",
            "endpoints_document": str(missing),
        },
    )

    assert entry["serving"] == "unknown"
    assert str(missing) in entry["serving_detail"]


def test_unknown_when_the_document_is_not_parsable(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")

    entry = _serving_report(
        tmp_path,
        {
            "launch": "cli",
            "command": "synthetic-worker",
            "endpoints_document": str(malformed),
        },
    )

    assert entry["serving"] == "unknown"
    assert str(malformed) in entry["serving_detail"]
