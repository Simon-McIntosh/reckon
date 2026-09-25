"""The crew MCP view exposes the derived obligations without reshaping them.

``_crew`` is a thin dispatcher, so the obligation view must hand back the
caller the same items :func:`reckon.crew.obligations.obligations` derives —
same kinds, ages and next commands — rather than a copy that can drift from the
function directly.

Run one file with the repo interpreter from the repo root::

    .venv/bin/python -m pytest tests/test_mcp_crew_obligations.py -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import reckon.mcp as mcp_module
from reckon.crew import recovery, runs

obligations_module = mcp_module.obligations_module

PROJECT = "obligation-view-fixture"
SESSION = "coordinator-view-fixture"


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic config home with one mounted project and no live pointers."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed obligation view fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _row(run_id: str, classification: str, age: int, command: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "session": SESSION,
        "plan": "fixture-plan",
        "node": f"node-{run_id}",
        "classification": classification,
        "recovery_classification": classification,
        "terminal_age_seconds": age,
        "next_action": command,
    }


@pytest.fixture()
def derived(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the classifier and the closure figure the derivation reads."""
    monkeypatch.setattr(
        recovery,
        "recover",
        lambda **_kwargs: {
            "runs": [
                _row(
                    "run-review-missing",
                    "scoring",
                    120,
                    "reckon crew dispatch --node review-run-review-missing",
                ),
                _row(
                    "run-review-ready",
                    "promotable",
                    240,
                    "reckon crew complete --run run-review-ready --gate <verdict>",
                ),
            ]
        },
    )
    monkeypatch.setattr(
        runs,
        "drain",
        lambda project, session=None: {
            "project": project,
            "session": session,
            "unreconciled_runs": 2,
        },
    )
    return obligations_module.obligations(PROJECT, SESSION)


def test_obligations_view_returns_the_derived_items_unchanged(
    config_home: Path, derived: dict[str, Any]
) -> None:
    result = mcp_module._crew(PROJECT, view="obligations", session=SESSION)

    assert result["ok"] is True
    assert result["view"] == "obligations"
    assert result["project"] == PROJECT
    assert result["session"] == SESSION
    assert "obligations" in result, (
        "the obligations view did not carry the derived payload"
    )
    assert result["obligations"] == derived["obligations"]
    assert result["summary"] == derived["summary"]
    assert [item["kind"] for item in result["obligations"]] == [
        "review-ready",
        "review-missing",
    ]


def test_obligations_view_refuses_a_missing_session(config_home: Path) -> None:
    result = mcp_module._crew(PROJECT, view="obligations")

    assert result["ok"] is False
    assert result["error"] == "missing_session"
    assert result["project"] == PROJECT
