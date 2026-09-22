"""verify-gate can be given the command it runs, so a coordinator can measure
a suite wider than the node's own recorded gate at the merged head."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon.cli import main as cli_main

PROJECT = "proj"
PLAN = "plan-a"
STORED_COMMAND = "sh gate.sh"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    # The stored gate writes its own marker, so a re-run that executes it is
    # distinguishable from one that executes a supplied command instead.
    (root / "gate.sh").write_text(
        "#!/bin/sh\necho stored > stored.marker\n", encoding="utf-8"
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "gate.sh", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _promote(repository: Path, run_id: str, *, gate_check: dict | None) -> None:
    record: dict = {"run_id": run_id, "gate": "passed"}
    if gate_check is not None:
        record["gate_check"] = gate_check
    ledger.append_run(PROJECT, record, root=repository)


def _stored_gate() -> dict:
    return {"command": STORED_COMMAND, "exit_status": 0, "log_digest": "x"}


def _invoke(repository: Path, run_id: str, *extra: str):
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "verify-gate",
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--checkout-path",
            str(repository),
            *extra,
        ],
    )


def test_verify_gate_runs_the_supplied_command_not_the_stored_one(
    repository: Path,
) -> None:
    """Given --command, the supplied command runs in place of the stored one,
    and the report records which of the two was used."""
    run_id = "r-20260922T170000000000-verify-gate-supplied"
    _promote(repository, run_id, gate_check=_stored_gate())
    supplied = "echo supplied > supplied.marker"

    result = _invoke(repository, run_id, "--command", supplied)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    report = payload["report"]
    assert report["gate_command"] == supplied
    assert report["gate_command_source"] == "option"
    # The supplied command actually executed, and the stored one did not: the
    # markers are the executed-command evidence, not the field's own value.
    assert (repository / "supplied.marker").is_file()
    assert not (repository / "stored.marker").exists()


def test_verify_gate_without_the_option_runs_the_stored_command(
    repository: Path,
) -> None:
    """Without --command, the stored gate command runs exactly as before."""
    run_id = "r-20260922T170001000000-verify-gate-stored"
    _promote(repository, run_id, gate_check=_stored_gate())

    result = _invoke(repository, run_id)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    report = payload["report"]
    assert report["gate_command"] == STORED_COMMAND
    assert report["gate_command_source"] == "stored"
    assert report["integrated_verdict"] == "passed"
    assert report["finding"] is None
    assert (repository / "stored.marker").is_file()
    assert not (repository / "supplied.marker").exists()


def test_recorded_report_names_the_command_that_actually_ran(repository: Path) -> None:
    """When the supplied command differs from the stored one, the report
    recorded on the run's committed row names the command that executed."""
    run_id = "r-20260922T170002000000-verify-gate-recorded"
    _promote(repository, run_id, gate_check=_stored_gate())
    supplied = "echo supplied > supplied.marker"

    result = _invoke(repository, run_id, "--command", supplied)

    assert result.exit_code == 0, result.output
    row = ledger.load(PROJECT, root=repository)[0]["runs"][0]
    recorded = row["integrated_gate_check"]
    assert recorded["gate_command"] == supplied
    assert recorded["gate_command"] != STORED_COMMAND
    assert recorded["gate_command_source"] == "option"


def test_verify_gate_accepts_a_command_on_a_run_with_no_stored_gate(
    repository: Path,
) -> None:
    """A run that stored no gate command is measured rather than refused for
    the absence: the supplied command supplies the capability that was missing."""
    run_id = "r-20260922T170003000000-verify-gate-nostored"
    _promote(repository, run_id, gate_check=None)
    supplied = "echo supplied > supplied.marker"

    result = _invoke(repository, run_id, "--command", supplied)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    report = payload["report"]
    assert report["ran"] is True
    assert report["reason"] is None
    assert report["integrated_verdict"] == "passed"
    assert report["gate_command"] == supplied
    assert report["gate_command_source"] == "option"
    assert (repository / "supplied.marker").is_file()
