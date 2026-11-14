"""A route edit that names a backend keeps that backend.

``crew queue edit --backend <name> --model <name>`` is a deterministic route:
which backend runs the pair is the coordinator's decision. Two backends may
serve the same lane and model key, such as a lane's plain backend and an
editing variant of it, so resolving the model on its own returns whichever
backend the configuration lists first and silently replaces the one named.
These tests hold the edit to the named backend, and to a refusal naming both
when that backend does not serve the model.

The negative control resolves the model to its first defining backend, which
substitutes the other backend in the first test.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

crew_dispatch_commands = importlib.import_module("reckon.crew_dispatch_commands")

CREW = crew_dispatch_commands.crew
PROJECT = "reckon"

#: One lane, ``codex``, served by two backends: the plain one is declared first
#: so a resolver that returns the first backend serving the pair returns it.
CATALOGUE: dict = {
    "version": 1,
    "lanes": {
        "codex": {
            "default_model": "sol",
            "models": {"sol": {"model": "gpt-6-sol", "effort": "high"}},
        },
        "claude": {
            "default_model": "sonnet",
            "models": {"sonnet": {"model": "claude-sonnet-5-5", "effort": "medium"}},
        },
    },
    "aliases": {},
}

CONFIG: dict = {
    "default_backend": "codex",
    "local_backend": "codex",
    "backends": {
        "codex": {
            "launch": "cli",
            "model": "gpt-6-sol",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "codex",
            "model_key": "sol",
        },
        "codex-anchored": {
            "launch": "cli",
            "model": "gpt-6-sol",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "codex",
            "model_key": "sol",
            "anchored_edit": True,
        },
        "claude": {
            "launch": "cli",
            "model": "claude-sonnet-5-5",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "lane": "claude",
            "model_key": "sonnet",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

NODE: dict = {
    "id": "route-edit-node",
    "goal": "Keep the backend a route edit names.",
    "plan": "a-plan",
    "section": "a-section",
    "brief": "",
    "brief_sha256": "",
    "brief_path": "",
    "role": "implement",
    "spec_level": "exact",
    "done_when": "pytest -q tests/test_queue_edit_keeps_the_named_backend.py reports 2 passed.",
    "write_paths": ["reckon/crew/queue_edit.py"],
    "time_budget": "20m",
    "manifest_path": "",
    "negative_control": "none: the node writes no check",
    "estimated_hours": 1.0,
    "requires_decisions": [],
    "peer_scopes": {},
    "regenerate_paths": [],
    "images": [],
    "inputs": [],
    "compares_with": "",
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic configuration home with one mounted project and a catalogue."""
    home = tmp_path / "config"
    for directory in ("crew/live", "crew/locks", "crew/runs", "crew/discards"):
        (home / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    catalogue = tmp_path / "catalogue.yaml"
    catalogue.write_text(yaml.safe_dump(CATALOGUE), encoding="utf-8")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue))
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: CONFIG
    )
    return home


def _write_request(home: Path, run_id: str = "r-queued") -> Path:
    """Store one queued request's pointer."""
    node = dict(NODE)
    node["manifest_path"] = str(home.parent / "manifest.md")
    pointer = home / "crew" / "live" / f"{run_id}.json"
    pointer.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "plan": "a-plan",
                "section": "a-section",
                "repo": str(home / "repo"),
                "session": "session-1",
                "node": node,
                "backend": "codex",
                "launch": "cli",
                "local": True,
                "phase": "queued",
                "queued_at": "2026-11-14T05:00:00+00:00",
                "created_at": "2026-11-14T05:00:00+00:00",
                "dispatch_options": {"locked_decisions": []},
            }
        ),
        encoding="utf-8",
    )
    return pointer


def _pointer(home: Path, run_id: str = "r-queued") -> dict[str, Any]:
    return json.loads(
        (home / "crew" / "live" / f"{run_id}.json").read_text(encoding="utf-8")
    )


def _answer(result) -> dict[str, Any]:
    text = result.output.strip()
    try:
        return json.loads(text)
    except ValueError:
        return {"ok": False, "detail": text.removeprefix("Error: ")}


def test_an_edit_keeps_the_backend_it_names(home: Path) -> None:
    """Two backends serve the requested pair; the one named is the one stored."""
    _write_request(home)

    result = CliRunner().invoke(
        CREW,
        [
            "queue",
            "edit",
            "--run",
            "r-queued",
            "--backend",
            "codex-anchored",
            "--model",
            "sol",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _answer(result)
    record = _pointer(home)
    assert record["backend"] == "codex-anchored"
    assert record["requested_backend"] == "codex-anchored"
    assert record["lane"] == "codex"
    assert record["model_key"] == "sol"
    assert record["named_route"]["backend"] == "codex-anchored"
    assert payload["route"]["backend"] == "codex-anchored"


def test_a_model_the_named_backend_does_not_serve_is_refused(home: Path) -> None:
    """A model outside the named backend's lane is refused, naming both."""
    pointer = _write_request(home)
    before = pointer.read_bytes()

    result = CliRunner().invoke(
        CREW,
        [
            "queue",
            "edit",
            "--run",
            "r-queued",
            "--backend",
            "codex-anchored",
            "--model",
            "sonnet",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "codex-anchored" in result.output
    assert "sonnet" in result.output
    assert pointer.read_bytes() == before
