"""A picker route launches the model it chose, not only its backend.

The picker names a lane's own backend with the chosen model beside it, so a
pick of ``codex`` and ``astra`` must launch astra rather than the lane's
default sol. The model is applied exactly as ``--model`` narrows a named pair:
it reaches the entry that serves it, the launch argv carries its model id, and
the run record's ``model_key`` reads back the model key. A model the lane does
not declare is refused by name rather than launched as the lane default.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from reckon import _backends, cli, ledger
from reckon import flight as flight_module

picker_module = importlib.import_module("reckon.crew.picker")

DONE_WHEN = "pytest tests/test_a_picked_model_is_the_model_launched.py passes"

# A codex lane with two models. ``codex-astra`` is declared among the backends
# so lane expansion creates one entry per model, which is the shape the host
# layer resolves and the entry a picked model is applied through.
FLIGHT = """\
routing:
  picker: route
default_backend: codex
backends:
  codex-astra: {}
lanes:
  codex:
    launch: cli
    command: codex
    sandbox: worktree-full
    time_budget: 20m
    default_model: sol
    models:
      sol:
        model: gpt-6-sol
      astra:
        model: gpt-6-astra
roles:
  implement:
    execution_capable: true
    sandbox: worktree-full
    time_budget: 20m
fences:
  time_budget: 20m
  needs_help_after_failures: 2
"""

# The model catalogue resolves a recorded model id back to its lane and key, so
# the run record's ``model_key`` carries ``astra`` beside the id the argv holds.
# ``astra-legacy`` is a catalogue-only historical alias: the flight config names
# no such alias on any backend, so only a resolver that consults the catalogue
# reaches the entry it points at.
CATALOGUE = {
    "version": 1,
    "lanes": {
        "codex": {
            "default_model": "sol",
            "models": {
                "sol": {"model": "gpt-6-sol"},
                "astra": {"model": "gpt-6-astra"},
            },
        }
    },
    "aliases": {"astra-legacy": {"lane": "codex", "model_key": "astra"}},
}


def _selection(action: str, backend: str | None, model: str | None = None):
    """A picker answer shaped like the object ``dispatch_picker_selection`` reads."""
    fields = {
        "action": action,
        "backend": backend,
        "family": "cli",
        "model": model,
        "effort": "high",
        "probabilities": {backend: 0.9} if backend else {"hold": 0.9},
        "confidence": 0.9,
        "jev_model": "jev-test",
        "fallback_reason": None,
        "latency_ms": 1.0,
        "excluded": [],
    }
    return SimpleNamespace(as_dict=lambda: fields)


@pytest.fixture()
def repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose docs tree carries a plan and a writable target."""
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
    catalogue = tmp_path / "catalogue.yaml"
    catalogue.write_text(yaml.safe_dump(CATALOGUE), encoding="utf-8")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue))
    return root


def _arguments(repo: Path) -> list[str]:
    return [
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
        "picked-model",
        "--goal",
        "launch the model the picker chose",
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "target.py",
        "--session",
        "picked-model-session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
    ]


def _install_picker(monkeypatch: pytest.MonkeyPatch, selection) -> None:
    monkeypatch.setattr(picker_module, "pick", lambda *_a, **_k: selection)


def test_a_picked_model_reaches_the_launch_argv(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pick of codex and astra launches gpt-6-astra, not the lane default."""
    (home / "flight.yaml").write_text(FLIGHT, encoding="utf-8")
    _install_picker(monkeypatch, _selection("route", "codex", "astra"))

    result = CliRunner().invoke(cli.main, _arguments(repo))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "codex-astra"
    assert payload["agent"]["model"] == "gpt-6-astra"
    assert payload["picker_selection"]["backend"] == "codex"
    assert payload["picker_selection"]["model"] == "astra"

    config = flight_module.resolve("sample", checkout_path=repo).config
    # The launch plan is built from the backend dispatch actually resolved, not
    # the literal the pick named: a regression that resolved the lane default
    # would compose the default's argv, and this assertion would catch it.
    resolved = payload["backend"]
    plan = _backends.launch_plan(
        backend_name=resolved,
        backend=config["backends"][resolved],
        prompt="p",
        worktree=str(repo),
        fence=False,
    )
    assert plan.argv[plan.argv.index("-m") + 1] == "gpt-6-astra"


def test_the_picked_model_is_recorded_on_the_run(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run record's model key reads back the model the picker chose."""
    (home / "flight.yaml").write_text(FLIGHT, encoding="utf-8")
    _install_picker(monkeypatch, _selection("route", "codex", "astra"))

    result = CliRunner().invoke(cli.main, _arguments(repo))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    row = ledger.build_record(
        run_id="r-picked-model",
        plan="sample",
        gate="passed",
        backend=payload["backend"],
        agent=payload["agent"],
    )
    assert (row["lane"], row["model_key"]) == ("codex", "astra")


def test_a_model_the_lane_does_not_declare_is_refused(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pick naming a model outside the lane is refused, naming both."""
    (home / "flight.yaml").write_text(FLIGHT, encoding="utf-8")
    _install_picker(monkeypatch, _selection("route", "codex", "ghost"))

    result = CliRunner().invoke(cli.main, _arguments(repo))

    assert result.exit_code != 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["ok"] is False
    assert "codex" in payload["detail"]
    assert "ghost" in payload["detail"]


def _deterministic_arguments(repo: Path, backend: str, model: str) -> list[str]:
    """A dispatch that resolves the named pair through ``--model``.

    ``--route deterministic`` keeps the picker off the path so the ``--model``
    narrowing is the path under test, exactly as a caller naming a lane and a
    model on the command line reaches it.
    """
    return [
        *_arguments(repo),
        "--route",
        "deterministic",
        "--backend",
        backend,
        "--model",
        model,
    ]


def test_a_catalogue_alias_reaches_the_same_backend_on_both_routes(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue-only alias resolves to the same backend and model on both the
    ``--model`` route and the picker route.

    The alias is declared only in the catalogue, never on a flight backend, so
    the two routes agree only when each narrows through the one catalogue
    resolver rather than matching flight fields by hand.
    """
    (home / "flight.yaml").write_text(FLIGHT, encoding="utf-8")

    named = CliRunner().invoke(
        cli.main, _deterministic_arguments(repo, "codex", "astra-legacy")
    )
    assert named.exit_code == 0, named.output
    named_payload = json.loads(named.output.splitlines()[0])

    _install_picker(monkeypatch, _selection("route", "codex", "astra-legacy"))
    picked = CliRunner().invoke(cli.main, _arguments(repo))
    assert picked.exit_code == 0, picked.output
    picked_payload = json.loads(picked.output.splitlines()[0])

    assert named_payload["backend"] == "codex-astra"
    assert picked_payload["backend"] == named_payload["backend"]
    assert picked_payload["agent"]["model"] == named_payload["agent"]["model"]
    assert named_payload["agent"]["model"] == "gpt-6-astra"


def test_an_undeclared_pair_is_refused_identically_on_both_routes(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both routes refuse an undeclared pair with the same message."""
    (home / "flight.yaml").write_text(FLIGHT, encoding="utf-8")

    named = CliRunner().invoke(
        cli.main, _deterministic_arguments(repo, "codex", "ghost")
    )
    assert named.exit_code != 0, named.output
    named_refusal = json.loads(named.output.splitlines()[0])

    _install_picker(monkeypatch, _selection("route", "codex", "ghost"))
    picked = CliRunner().invoke(cli.main, _arguments(repo))
    assert picked.exit_code != 0, picked.output
    picked_refusal = json.loads(picked.output.splitlines()[0])

    assert named_refusal["detail"] == picked_refusal["detail"]
    assert "codex" in named_refusal["detail"]
    assert "ghost" in named_refusal["detail"]


def test_both_callers_narrow_through_the_shared_resolver(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither ``--model`` nor the picker route keeps its own narrowing code.

    The shared resolver is patched with a spy that records each call and
    delegates, so a caller that re-implemented the narrowing would resolve the
    pair without reaching the spy and the recorded calls would not appear.
    """
    (home / "flight.yaml").write_text(FLIGHT, encoding="utf-8")
    calls: list[tuple[str | None, str]] = []
    real = ledger.backend_serving_model

    def spy(config, *, lane, model):
        calls.append((lane, model))
        return real(config, lane=lane, model=model)

    monkeypatch.setattr(ledger, "backend_serving_model", spy)

    _install_picker(monkeypatch, _selection("route", "codex", "astra"))
    picked = CliRunner().invoke(cli.main, _arguments(repo))
    assert picked.exit_code == 0, picked.output
    assert ("codex", "astra") in calls

    calls.clear()
    named = CliRunner().invoke(
        cli.main, _deterministic_arguments(repo, "codex", "astra")
    )
    assert named.exit_code == 0, named.output
    assert ("codex", "astra") in calls
