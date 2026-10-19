"""A lane named with no model lets Jev choose the model inside it.

Both paths that name a lane without naming a model -- a coordinator's
``--backend`` and the review reflex's composed lane -- reach the same owner, so
both put the lane's own models to the picker and launch the model the answer
names. The lane still never changes: a hold, a raise, a fallback and a model of
another lane all launch the lane default, and a lane declaring no choice, a
``--model``, a ``--local`` and a paired arm ask nothing at all.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from reckon import cli, crew
from reckon import flight as flight_module

picker_module = importlib.import_module("reckon.crew.picker")
dispatch_module = importlib.import_module("reckon.crew.dispatch")

DONE_WHEN = "pytest tests/test_a_named_lane_lets_jev_choose_its_model.py passes"

# A codex lane declaring three models. ``codex-astra`` and ``codex-luna`` are
# named among the backends so lane expansion creates one entry per model, the
# shape a host layer resolves and the entry an applied model is read back from.
# No ``roles.<role>.by_spec_level`` overlay is declared, so each model launches
# at the effort it declares -- luna at xhigh, distinct from the lane default's
# sol at high, which is what tells an applied pick from an ignored one.
FLIGHT = """\
routing:
  picker: route
default_backend: codex
local_backend: clive
backends:
  codex-astra: {}
  codex-luna: {}
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
        effort: high
      astra:
        model: gpt-6-astra
        effort: medium
      luna:
        model: gpt-6-luna
        effort: xhigh
  clive:
    launch: cli
    command: codex
    sandbox: worktree-full
    time_budget: 20m
    default_model: sol
    models:
      sol:
        model: gpt-6-clive
        effort: high
roles:
  implement:
    execution_capable: true
    sandbox: worktree-full
    time_budget: 20m
  review:
    execution_capable: true
    sandbox: worktree-full
    time_budget: 20m
fences:
  time_budget: 20m
  needs_help_after_failures: 2
"""

# The catalogue backs the identity a recorded model id resolves to, so a run
# record read back names the model key the launch carried.
CATALOGUE = {
    "version": 1,
    "lanes": {
        "codex": {
            "default_model": "sol",
            "models": {
                "sol": {"model": "gpt-6-sol"},
                "astra": {"model": "gpt-6-astra"},
                "luna": {"model": "gpt-6-luna"},
            },
        },
        "clive": {
            "default_model": "sol",
            "models": {"sol": {"model": "gpt-6-clive"}},
        },
    },
    "aliases": {},
}


class _Answer:
    """A picker answer shaped like the object ``dispatch_picker_selection`` reads."""

    def __init__(self, action, backend, model=None, family="codex"):
        self._fields = {
            "action": action,
            "backend": backend,
            "family": family,
            "model": model,
            "effort": "high",
            "probabilities": {backend: 0.9} if backend else {"hold": 0.9},
            "confidence": 0.9,
            "jev_model": "jev-test",
            "fallback_reason": None,
            "latency_ms": 1.0,
            "excluded": [],
        }

    def as_dict(self):
        return dict(self._fields)


def _route(backend="codex", model="luna", family="codex"):
    return _Answer("route", backend, model, family)


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
    (home / "flight.yaml").write_text(FLIGHT, encoding="utf-8")
    catalogue = tmp_path / "catalogue.yaml"
    catalogue.write_text(yaml.safe_dump(CATALOGUE), encoding="utf-8")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue))
    return root


def _arguments(repo: Path, *, node: str, extra: list[str] | None = None) -> list[str]:
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
        node,
        "--goal",
        "launch the model the lane let Jev choose",
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "target.py",
        "--session",
        "named-lane-session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
        "--backend",
        "codex",
        *(extra or []),
    ]


def _install_picker(monkeypatch, answer_factory, offered: list | None = None):
    def pick(request, config, *, repo, cached_only, **inputs):
        if offered is not None:
            offered.append(sorted(config.get("backends") or {}))
        selected = (
            answer_factory(config) if callable(answer_factory) else answer_factory
        )
        if isinstance(selected, Exception):
            raise selected
        return selected

    monkeypatch.setattr(picker_module, "pick", pick)


def test_a_named_lane_launches_the_model_jev_names(repo, monkeypatch):
    """``--backend codex`` launches luna, at luna's effort, and records it."""
    offered: list[list[str]] = []
    _install_picker(monkeypatch, _route(model="luna"), offered)

    result = CliRunner().invoke(cli.main, _arguments(repo, node="named-lane-luna"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "codex"
    assert payload["agent"]["model"] == "gpt-6-luna"
    assert payload["agent"]["effort"] == "xhigh"
    assert payload["route"] == "deterministic"
    # The ask is offered the named lane's backends and no others.
    assert offered == [["codex", "codex-astra", "codex-luna"]]


def test_the_named_lane_choice_is_recorded_on_the_run(repo, monkeypatch):
    """A real dispatch of ``--backend codex`` records route mode and model key."""
    _install_picker(monkeypatch, _route(model="luna"))
    real_dispatch = dispatch_module.dispatch

    def dispatch_with_launcher(*args, **kwargs):
        kwargs.setdefault("launcher", lambda *_a, **_k: os.getpid())
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(crew, "dispatch", dispatch_with_launcher)

    arguments = [
        a for a in _arguments(repo, node="named-lane-record") if a != "--dry-run"
    ]
    result = CliRunner().invoke(cli.main, arguments)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["route_mode"] == "explicit"
    assert payload["model_key"] == "luna"
    assert payload["agent"]["model"] == "gpt-6-luna"
    assert payload["picker_selection"]["model"] == "luna"


@pytest.mark.parametrize(
    "answer",
    [
        _Answer("hold", None, model=None),
        RuntimeError("picker exploded"),
        _Answer("route", "codex", model="claude-sonnet-5-5", family="claude"),
    ],
    ids=["hold", "raises", "other-lane"],
)
def test_a_failed_ask_launches_the_lane_default(repo, monkeypatch, answer):
    """A hold, a raise and another lane's model all launch the default sol."""
    _install_picker(monkeypatch, answer)
    node = "named-lane-defect-" + type(answer).__name__

    result = CliRunner().invoke(cli.main, _arguments(repo, node=node))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["backend"] == "codex"
    assert payload["agent"]["model"] == "gpt-6-sol"


def test_no_ask_when_a_model_is_named(repo, monkeypatch):
    """``--backend codex --model astra`` names the model and asks nothing."""
    calls: list[str] = []

    def pick(*_a, **_k):
        calls.append("asked")
        raise AssertionError("the picker must not be asked when a model is named")

    monkeypatch.setattr(picker_module, "pick", pick)
    result = CliRunner().invoke(
        cli.main,
        _arguments(repo, node="named-lane-model", extra=["--model", "astra"]),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[0])
    assert payload["agent"]["model"] == "gpt-6-astra"
    assert calls == []


def test_no_ask_for_a_local_dispatch(repo, monkeypatch):
    """``--local`` names the local lane, which offers no choice to make."""
    calls: list[str] = []

    def pick(*_a, **_k):
        calls.append("asked")
        raise AssertionError("the picker must not be asked for a --local dispatch")

    monkeypatch.setattr(picker_module, "pick", pick)
    CliRunner().invoke(
        cli.main, _arguments(repo, node="named-lane-local", extra=["--local"])
    )

    # The refusal (if the local lane is unavailable) is beside the point: the
    # assertion is that the ask never ran, which is what calls records.
    assert calls == []


def test_no_ask_for_a_one_model_lane(repo, monkeypatch):
    """A lane declaring a single model offers nothing to choose between."""
    home = repo.parent / "config"
    (home / "flight.yaml").write_text(
        FLIGHT.replace(
            """    models:
      sol:
        model: gpt-6-sol
        effort: high
      astra:
        model: gpt-6-astra
        effort: medium
      luna:
        model: gpt-6-luna
        effort: xhigh
""",
            """    models:
      sol:
        model: gpt-6-sol
        effort: high
""",
        ).replace("  codex-astra: {}\n  codex-luna: {}\n", ""),
        encoding="utf-8",
    )
    calls: list[str] = []

    def pick(*_a, **_k):
        calls.append("asked")
        raise AssertionError("a one-model lane must not ask the picker")

    monkeypatch.setattr(picker_module, "pick", pick)
    CliRunner().invoke(cli.main, _arguments(repo, node="named-lane-single"))

    assert calls == []


def test_a_composed_review_of_a_codex_run_launches_the_model_jev_names(
    repo, monkeypatch
):
    """The review reflex's named lane asks the same owner and launches luna."""
    from reckon.crew import recovery, runs

    _install_picker(monkeypatch, _route(model="luna"))
    manifest = repo.parent / "config" / "manifests" / "r-reviewed.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: r-reviewed\nstatus: complete\ncommits: r-reviewed\n", encoding="utf-8"
    )
    record = {
        "run_id": "r-reviewed",
        "project": "sample",
        "repo": str(repo),
        "node": {"id": "r-reviewed", "plan": "fixture", "section": "guard"},
        "backend": "codex",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "named-lane-session",
        "manifest_path": str(manifest),
    }
    runs._write_json(runs.pointer_path("r-reviewed"), record)

    captured: dict = {}

    def stub_dispatch(*_args, **kwargs):
        captured.update(kwargs)
        return {"run_id": "r-review"}

    monkeypatch.setattr(dispatch_module, "dispatch", stub_dispatch)

    offered: list[list[str]] = []
    _install_picker(monkeypatch, _route(model="luna"), offered)

    config = flight_module.resolve("sample", checkout_path=repo).config
    with runs.follower_claim("sample", "named-lane-session", delivery="stream"):
        report = recovery.dispatch_review_for_run(record, config=config)

    assert report["dispatched"] is True, report
    assert captured["config"]["backends"]["codex"]["model"] == "gpt-6-luna"
    assert captured["picker_selection"]["model"] == "luna"
    assert offered == [["codex", "codex-astra", "codex-luna"]]
