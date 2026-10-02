"""A withdrawn run completes as withdrawn, never on an empty project name.

A dispatch whose launch is refused at admission leaves a run that names no
project: the pointer never carries one, or a run directory left by the refusal
holds no supervisor record for reconstruction to read one from. ``crew
complete`` resolved such a run and then validated the empty project name, which
failed before the run could be reported as the withdrawal it is. Each case here
builds a withdrawn run in a temporary RECKON_HOME and asserts that completion
reports the withdrawal, writes no ledger row, and names the empty project
explicitly rather than raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.runs import _write_json, pointer_path, run_dir


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated config home and a caller-owned checkout path."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return tmp_path


def _no_ledger_written(config_home: Path) -> bool:
    """Whether no crew ledger file was created anywhere under the config home."""
    return not any(path.name == "crew.json" for path in config_home.rglob("*.json"))


def test_a_pointerless_withdrawn_run_completes_as_withdrawn(home: Path) -> None:
    """No pointer, no project: the run reports withdrawn and writes no row."""
    run_id = "r-20261001T230854840625-review-of-ctta-explanatory-figure"
    directory = run_dir(run_id)
    directory.mkdir(parents=True)
    # The refusal leaves only its classification record: no supervisor, no
    # worker, no manifest, so reconstruction resolves no project from it.
    (directory / "classification.json").write_text(
        json.dumps({"inputs": {}, "key": "x", "version": 1}),
        encoding="utf-8",
    )
    assert not pointer_path(run_id).exists()

    result = crew.complete(
        run_id,
        gate="not-run",
        outcome="the launch was refused before a project was resolved",
        root=home / "repo",
    )

    assert result["withdrawn"] is True
    assert result["status"] == "withdrawn"
    assert result["project"] == ""
    assert result["ledger_row_written"] is False
    assert result["promoted"] is False
    assert result["record"]["rebuilt_from_run_directory"] is True
    assert _no_ledger_written(home / "config")


def test_a_pointer_with_no_project_completes_as_withdrawn(home: Path) -> None:
    """A surviving pointer that names no project is a withdrawal too."""
    run_id = "r-20261001T230900000000-refused-at-admission"
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "",
            "repo": "",
            "worktree": "",
            "launch": "cli",
            "role": "review",
            "created_at": "2026-10-01T23:09:00Z",
            "node": {},
        },
    )

    result = crew.complete(
        run_id,
        gate="not-run",
        outcome="the launch was refused before a project was resolved",
        root=home / "repo",
    )

    assert result["withdrawn"] is True
    assert result["status"] == "withdrawn"
    assert _no_ledger_written(home / "config")
