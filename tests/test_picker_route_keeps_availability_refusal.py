"""The picker route applies the deterministic availability refusal to its backend.

Under ``routing.picker: route`` the deterministic model-availability refusal
used to be skipped outright, so a dispatch whose picker resolved a backend the
account does not serve sailed past the exit-5 competence refusal the
deterministic route would have given it. The picker's own filter reads only
cached observations, and a model with no cached probe reads "unknown" and stays
eligible, so the absence of the deterministic check is what let an unserved
backend through.

These cases drive the three ways a picker answer reaches a backend — a fallback
to the unserved default, a route to a named unserved backend, and a route to a
served one — and require the deterministic refusal on the first two and a clean
dispatch on the third.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_picker as dispatch_picker_module
from reckon import cli as cli_module
from reckon import crew

picker_module = importlib.import_module("reckon.crew.picker")
dispatch_module = importlib.import_module("reckon.crew.dispatch")

DONE_WHEN = "pytest tests/test_picker_route_keeps_availability_refusal.py passes"

CATALOG_SCRIPT = "#!/bin/sh\nprintf '%s\\n' 'served-alpha gpu-a' 'served-beta gpu-b'\n"

UNSERVED_MODEL = "unserved-gamma"
SERVED_MODEL = "served-alpha"


def _host_flight() -> str:
    """Two cli backends sharing one catalog: one served, one unserved.

    The catalog lists served-alpha and served-beta only, so the ``worker``
    backend's configured ``unserved-gamma`` reads as not served and the
    ``server`` backend's ``served-alpha`` reads as served.
    """
    return (
        "routing:\n"
        "  picker: route\n"
        "default_backend: worker\n"
        "backends:\n"
        "  worker:\n"
        "    launch: cli\n"
        "    command: synthetic-worker\n"
        f"    model: {UNSERVED_MODEL}\n"
        "    effort: high\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "    catalog:\n"
        '      list_command: ["synthetic-worker", "--list"]\n'
        "      model_pattern: '^{model}\\b'\n"
        "  server:\n"
        "    launch: cli\n"
        "    command: synthetic-worker\n"
        f"    model: {SERVED_MODEL}\n"
        "    effort: high\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "    catalog:\n"
        '      list_command: ["synthetic-worker", "--list"]\n'
        "      model_pattern: '^{model}\\b'\n"
        "roles:\n"
        "  implement:\n"
        "    backend: worker\n"
        "    execution_capable: true\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "fences:\n"
        "  time_budget: 20m\n"
        "  needs_help_after_failures: 2\n"
    )


def _in_harness_flight() -> str:
    """Two in-harness backends, both served, for a dispatch that can complete.

    An in-harness backend needs no external command and reports no unserved
    model, so the availability check passes and the dispatch runs to a
    directive — the path the single-pick check is asserted on.
    """
    return (
        "routing:\n"
        "  picker: route\n"
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: in-harness\n"
        "    model: alpha-model\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "  beta:\n"
        "    launch: in-harness\n"
        "    model: beta-model\n"
        "    sandbox: worktree-full\n"
        "    time_budget: 20m\n"
        "roles:\n"
        "  implement: {}\n"
        "fences:\n"
        "  time_budget: 20m\n"
        "  needs_help_after_failures: 2\n"
    )


def _selection(action: str, backend: str | None):
    """A picker answer shaped like the object ``dispatch_picker_selection`` reads."""
    fields = {
        "action": action,
        "backend": backend,
        "family": "cli",
        "model": None,
        "effort": "high",
        "probabilities": {backend: 0.9} if backend else {"hold": 0.9},
        "confidence": 0.9,
        "jev_model": "jev-test",
        "fallback_reason": None if action != "fallback" else "jev-error: ValueError",
        "latency_ms": 1.0,
        "excluded": [],
    }
    return SimpleNamespace(as_dict=lambda: fields)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose backend command is a synthetic catalog on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    worker = bin_dir / "synthetic-worker"
    worker.write_text(CATALOG_SCRIPT, encoding="utf-8")
    worker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
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
    return root


def _arguments(repo: Path, *, dry_run: bool) -> list[str]:
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
        "route one node and refuse an unserved backend",
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "target.py",
        "--session",
        "picker-availability-session",
        "--repo",
        str(repo),
        "--no-watch",
    ]
    if dry_run:
        arguments.append("--dry-run")
    return arguments


def _install_picker(monkeypatch: pytest.MonkeyPatch, selection) -> None:
    monkeypatch.setattr(picker_module, "pick", lambda *_a, **_k: selection)


def test_a_picker_fallback_to_an_unserved_backend_refuses(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run whose picker falls back to the unserved default exits 5."""
    (home / "flight.yaml").write_text(_host_flight(), encoding="utf-8")
    _install_picker(monkeypatch, _selection("fallback", None))

    result = CliRunner().invoke(cli_module.main, _arguments(repo, dry_run=True))

    assert result.exit_code == 5, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "competence-refusal"
    assert payload["competence"]["refusal"] == "model-unavailable"
    assert payload["competence"]["backend"] == "worker"
    assert payload["picker_selection"]["action"] == "fallback"
    assert not list(crew.list_live(project="sample"))


def test_a_picker_route_to_an_unserved_backend_refuses_and_names_it(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live dispatch the picker routes to an unserved backend exits 5."""
    (home / "flight.yaml").write_text(_host_flight(), encoding="utf-8")
    _install_picker(monkeypatch, _selection("route", "worker"))

    result = CliRunner().invoke(cli_module.main, _arguments(repo, dry_run=False))

    assert result.exit_code == 5, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "competence-refusal"
    assert payload["competence"]["backend"] == "worker"
    assert payload["competence"]["model"] == UNSERVED_MODEL
    assert payload["picker_selection"]["backend"] == "worker"
    assert not list(crew.list_live(project="sample"))


def test_a_picker_route_to_a_served_backend_still_dispatches(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route to a served backend passes the availability check and previews."""
    (home / "flight.yaml").write_text(_host_flight(), encoding="utf-8")
    _install_picker(monkeypatch, _selection("route", "server"))

    result = CliRunner().invoke(cli_module.main, _arguments(repo, dry_run=True))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["backend"] == "server"
    assert payload["picker_selection"]["backend"] == "server"


def test_a_picker_refusal_checks_the_unserved_default(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refuse continues to the default backend, so the probe runs on it."""
    (home / "flight.yaml").write_text(_host_flight(), encoding="utf-8")
    _install_picker(monkeypatch, _selection("refuse", None))

    result = CliRunner().invoke(cli_module.main, _arguments(repo, dry_run=True))

    assert result.exit_code == 5, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "competence-refusal"
    assert payload["competence"]["backend"] == "worker"
    assert payload["picker_selection"]["action"] == "refuse"
    assert not list(crew.list_live(project="sample"))


def test_one_pick_per_dispatch_and_the_checked_backend_is_dispatched(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher reuses the caller's pick, so it probes what it dispatches.

    A mock picker returns a different backend on a second call: were the pick
    made again inside ``dispatch`` the dispatched backend would be the second
    answer, which the availability check never saw. One call and a dispatched
    backend equal to the checked one is the contract.
    """
    (home / "flight.yaml").write_text(_in_harness_flight(), encoding="utf-8")
    calls: list[bool] = []

    def pick(*_args, **_kwargs):
        calls.append(True)
        return _selection("route", "alpha" if len(calls) == 1 else "beta")

    monkeypatch.setattr(picker_module, "pick", pick)

    result = CliRunner().invoke(cli_module.main, _arguments(repo, dry_run=False))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(calls) == 1, "the picker must be asked once per dispatch"
    assert payload["picker_selection"]["backend"] == "alpha"
    assert payload["backend"] == "alpha"


def test_the_cli_path_passes_precomputed_picker_inputs(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller builds the three inputs and hands them to the picker.

    The ledger rows, the verdict inputs and the budget snapshot are read before
    the picker thread starts, so the pick's own latency bound covers the pick
    rather than the reads. The dispatch is handed non-None inputs and no input
    errors, which is what proves the reads happened on the caller's side.
    """
    (home / "flight.yaml").write_text(_in_harness_flight(), encoding="utf-8")
    _install_picker(monkeypatch, _selection("route", "alpha"))
    captured: dict[str, object] = {}
    real = dispatch_module.dispatch_picker_selection

    def recording(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(dispatch_module, "dispatch_picker_selection", recording)

    result = CliRunner().invoke(cli_module.main, _arguments(repo, dry_run=True))

    assert result.exit_code == 0, result.output
    assert captured["records"] is not None
    assert captured["verdict_inputs"] is not None
    assert captured["budget_snapshot"] is not None
    assert not captured["input_errors"]


def test_an_input_build_failure_falls_back_naming_the_input(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed input read is a recorded fallback, never an exception.

    The verdict-input read is made to raise. The failure must be caught against
    the input that failed, so the picker is skipped, the default backend is
    used, and the fallback reason names ``verdict_inputs`` — the dispatch is
    never aborted before the picker is consulted.
    """
    (home / "flight.yaml").write_text(_in_harness_flight(), encoding="utf-8")
    _install_picker(monkeypatch, _selection("route", "alpha"))

    def boom(*_args, **_kwargs):
        raise RuntimeError("verdict store unavailable")

    monkeypatch.setattr(dispatch_picker_module, "_picker_verdict_inputs", boom)

    result = CliRunner().invoke(cli_module.main, _arguments(repo, dry_run=True))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    selection = payload["picker_selection"]
    assert selection["action"] == "fallback"
    assert "verdict_inputs" in selection["fallback_reason"]
    assert payload["backend"] == "alpha"
