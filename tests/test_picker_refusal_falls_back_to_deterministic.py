"""A picker that finds nothing eligible leaves the dispatch to deterministic routing.

Under ``routing.picker: route`` the picker's selection decides the backend, but
when it answers ``refuse`` — no eligible candidate and the default backend
ineligible — it names no backend the dispatch can use. A refusal must never make
a dispatch worse than the deterministic routing it replaces: the selection falls
through to the configured default backend, exactly as a ``fallback`` does, and
the deterministic gates then produce their own refusal or hold. These cases
drive the dispatch end to end so the exit code and payload are the ones the real
routing produced, not a restatement of them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import picker

DONE_WHEN = "pytest reports the refused selection resolving the deterministic backend"

LANE_GATE_REASON = "the engine relaunch is in progress"


def _refuse():
    """A picker answer that names no backend it can route to."""
    fields = {
        "action": "refuse",
        "backend": None,
        "family": None,
        "model": None,
        "effort": None,
        "probabilities": {},
        "confidence": None,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "reason": "no-eligible-candidates; default-backend-ineligible",
        "latency_ms": 1.0,
        "excluded": [],
    }
    return SimpleNamespace(as_dict=lambda: fields)


def _host_flight(*, codex_gate_document: Path | None = None) -> str:
    gate = (
        f"    gate_document: {codex_gate_document}\n"
        if codex_gate_document is not None
        else ""
    )
    return (
        "default_backend: codex\n"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: codex-fixture\n"
        "    model: fixture-model\n"
        "    effort: high\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "    budget_group: waves\n"
        f"{gate}"
        "roles:\n"
        "  implement:\n"
        "    backend: codex\n"
        "    execution_capable: true\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "fences:\n"
        "  time_budget: 20m\n"
        "  needs_help_after_failures: 2\n"
    )


def _write_flight(home: Path, *, codex_gate_document: Path | None = None) -> None:
    (home / "flight.yaml").write_text(
        _host_flight(codex_gate_document=codex_gate_document), encoding="utf-8"
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "target.py"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(root / "docs")}), encoding="utf-8"
    )
    _write_flight(home)
    return root


def _arguments(repo: Path, *, route: str | None = None) -> list[str]:
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "sample",
        "--plan",
        "fixture",
        "--section",
        "guard",
        "--role",
        "implement",
        "--spec-level",
        "exact",
        "--node",
        "candidate",
        "--goal",
        "resolve a refused picker selection",
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "target.py",
        "--session",
        "refusal-session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
    ]
    if route is not None:
        arguments += ["--route", route]
    return arguments


def _invoke(repo: Path, *, route: str | None = None):
    return CliRunner().invoke(cli_module.main, _arguments(repo, route=route))


def test_a_refused_selection_on_a_held_lane_gives_the_deterministic_hold(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refuse that finds nothing eligible yields the held-lane refusal.

    The picker names no backend, so the dispatch must fall through to the
    configured default exactly as deterministic routing would, and the lane
    gate — not the picker — decides the refusal. The routed arm therefore
    carries the same exit code and error key as the deterministic dispatch of
    the same node on the same held lane.
    """
    gate = home.parent / "router-gate.json"
    gate.write_text(
        json.dumps({"paused": True, "reason": LANE_GATE_REASON}), encoding="utf-8"
    )
    _write_flight(home, codex_gate_document=gate)
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: _refuse())

    routed = _invoke(repo)
    deterministic = _invoke(repo, route="deterministic")

    assert deterministic.exit_code == 75, deterministic.output
    assert json.loads(deterministic.output)["error"] == "lane-paused"

    assert routed.exit_code == deterministic.exit_code, routed.output
    payload = json.loads(routed.output)
    assert payload["error"] == "lane-paused"
    assert payload["lane_gate"]["reason"] == LANE_GATE_REASON


def test_a_refused_selection_with_open_gates_dispatches_to_the_default_backend(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every gate open, a refused selection resolves the default backend.

    The picker found nothing eligible, so dispatch continues as deterministic
    routing would: the configured default stands in, the node is dispatchable,
    and the refusal — with its reasons — stays recorded on the resolved run.
    """
    monkeypatch.setattr(picker, "pick", lambda *_a, **_k: _refuse())

    result = _invoke(repo)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["dry_run"] is True
    assert payload["backend"] == "codex"
    assert payload["route"] == "picker"
    assert payload["picker_selection"]["action"] == "refuse"
    assert "no-eligible-candidates" in payload["picker_selection"]["reason"]
