"""Dispatch refuses a selected backend whose declared model is not served."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew


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
