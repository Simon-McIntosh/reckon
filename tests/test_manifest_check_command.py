"""The manifest check reaches its writer while the run still holds a turn.

The same audit the promotion contract applies hours later, entered through the
command surface a worker can run itself. Each test drives the CLI runner rather
than the audit entry point, because what is under test is the wiring that gives
the audit its first production caller.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path

RUN_ID = "r-20260921T000000000000-manifest-check"

IN_FENCE_MANIFEST = """\
node: manifest-check
status: complete
commits: 1a2b3c4
changed_paths: reckon/cli.py
tests: uv run pytest tests/test_manifest_check_command.py -q -> 3 passed
test_logs: /tmp/manifest-check.log
artifacts: none
evidence_inputs: none
follow_ons: none
blockers: none
"""


@pytest.fixture()
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    path = tmp_path / "manifest.md"
    path.write_text(IN_FENCE_MANIFEST, encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "sample",
            "repo": str(tmp_path / "repo"),
            "worktree": str(tmp_path / "repo"),
            "base_sha": "0" * 40,
            "launch": "in-harness",
            "role": "implement",
            "node": {
                "id": "manifest-check",
                "plan": "fixture",
                "section": "s9",
                "write_paths": ["reckon/cli.py"],
            },
            "manifest_path": str(path),
        },
    )
    return path


def _check(run_id: str = RUN_ID):
    return CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", run_id])


def test_a_manifest_inside_its_fence_exits_zero_with_no_finding(manifest: Path) -> None:
    result = _check()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["findings"] == []


def test_the_command_names_the_run_it_read(manifest: Path) -> None:
    result = _check()

    assert RUN_ID in result.output
    assert json.loads(result.output)["run_id"] == RUN_ID


def test_a_path_outside_the_node_fence_is_reported_by_name(manifest: Path) -> None:
    manifest.write_text(
        IN_FENCE_MANIFEST.replace(
            "changed_paths: reckon/cli.py",
            "changed_paths: reckon/cli.py, reckon/other_module.py",
        ),
        encoding="utf-8",
    )

    result = _check()

    assert result.exit_code != 0, result.output
    findings = json.loads(result.output)["findings"]
    assert "reckon/other_module.py" in " ".join(findings)


def test_an_unknown_run_is_refused_rather_than_reported_clean(manifest: Path) -> None:
    result = _check(run_id="r-20260921T000000000000-absent")

    assert result.exit_code != 0
    assert result.output.strip()


def test_the_cli_is_the_only_production_caller() -> None:
    root = Path(cli_module.__file__).resolve().parent
    referencing = {
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if "audit_manifest" in path.read_text(encoding="utf-8")
    }

    assert referencing == {"cli.py", "crew.py", "crew/reports.py"}
