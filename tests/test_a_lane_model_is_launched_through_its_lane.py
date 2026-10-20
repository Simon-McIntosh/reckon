"""Dispatch uses a lane's declared model without a separate backend name."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from reckon import cli, crew, flight, ledger
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot

picker = importlib.import_module("reckon.crew.picker")

FLIGHT = """\
routing:
  picker: route
default_backend: codex
lanes:
  codex:
    launch: cli
    command: codex
    sandbox: worktree-full
    time_budget: 20m
    default_model: sol
    models:
      sol: {model: gpt-6-sol, effort: high}
      astra: {model: gpt-6-astra, effort: xhigh, time_budget: 55m}
      luna: {model: gpt-6-luna, effort: medium}
roles:
  implement:
    execution_capable: true
    sandbox: worktree-full
fences:
  time_budget: 60m
  needs_help_after_failures: 2
"""


@pytest.fixture
def lane_repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shipped = tmp_path / "shipped.yaml"
    shipped.write_text("{}\n")
    monkeypatch.setattr(flight, "shipped_defaults_path", lambda: shipped)
    root = tmp_path / "repo"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<!doctype html><html><head><meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture"></head>'
        '<body><h2 id="guard">Dispatch guard</h2></body></html>'
    )
    (root / "target.py").write_text("value = 1\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "target.py"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(root / "docs")}))
    (home / "flight.yaml").write_text(FLIGHT)
    catalogue = {
        "version": 1,
        "lanes": {
            "codex": {
                "default_model": "sol",
                "models": {
                    key: {"model": f"gpt-6-{key}"} for key in ("sol", "astra", "luna")
                },
            }
        },
    }
    catalogue_path = tmp_path / "catalogue.yaml"
    catalogue_path.write_text(yaml.safe_dump(catalogue))
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue_path))
    return root


def _args(repo: Path) -> list[str]:
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
        "lane-model",
        "--goal",
        "launch a declared model",
        "--done-when",
        "pytest tests/test_a_lane_model_is_launched_through_its_lane.py passes",
        "--write-path",
        "target.py",
        "--session",
        "lane-model-session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
    ]


def _pick(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    answer = {
        "action": "route",
        "backend": "codex",
        "family": "cli",
        "model": model,
        "effort": "high",
        "probabilities": {"codex": 0.9},
        "confidence": 0.9,
        "jev_model": "test",
        "fallback_reason": None,
        "latency_ms": 1.0,
        "excluded": [],
    }
    monkeypatch.setattr(
        picker, "pick", lambda *_a, **_k: SimpleNamespace(as_dict=lambda: answer)
    )


def _dispatch(args: list[str]) -> tuple[int, dict]:
    result = CliRunner().invoke(cli.main, args)
    return result.exit_code, json.loads(result.output.splitlines()[0])


def test_picked_astra_launches_through_codex(
    lane_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pick(monkeypatch, "gpt-6-astra")
    code, run = _dispatch(_args(lane_repo))
    assert code == 0, run
    assert run["backend"] == "codex"
    assert run["agent"]["model"] == "gpt-6-astra"
    assert run["agent"]["effort"] == "xhigh"
    assert run["time_budget"] == "55m"
    row = ledger.build_record(
        run_id="r-astra",
        plan="sample",
        gate="passed",
        backend=run["backend"],
        agent=run["agent"],
    )
    assert (row["lane"], row["model_key"]) == ("codex", "astra")


@pytest.mark.parametrize(
    ("key", "model", "effort", "budget"),
    [("sol", "gpt-6-sol", "high", "20m"), ("luna", "gpt-6-luna", "medium", "20m")],
)
def test_named_model_uses_its_lane_settings(
    lane_repo: Path, key: str, model: str, effort: str, budget: str
) -> None:
    code, run = _dispatch(
        [
            *_args(lane_repo),
            "--route",
            "deterministic",
            "--backend",
            "codex",
            "--model",
            key,
        ]
    )
    assert code == 0, run
    assert (
        run["backend"],
        run["agent"]["model"],
        run["agent"]["effort"],
        run["time_budget"],
    ) == (
        "codex",
        model,
        effort,
        budget,
    )


def test_undeclared_model_keeps_shared_refusal(
    lane_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named_code, named = _dispatch(
        [
            *_args(lane_repo),
            "--route",
            "deterministic",
            "--backend",
            "codex",
            "--model",
            "ghost",
        ]
    )
    _pick(monkeypatch, "ghost")
    picked_code, picked = _dispatch(_args(lane_repo))
    assert named_code != 0 and picked_code != 0
    assert named["detail"] == picked["detail"]


def test_dispatch_and_candidates_agree_for_each_model(
    lane_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = flight.resolve("sample", checkout_path=lane_repo).config
    assert "codex-astra" not in config["backends"]
    assert "codex-luna" not in config["backends"]
    request = PickRequest(
        "sample",
        TaskNode(
            id="work",
            goal="work",
            plan="",
            role="implement",
            spec_level="exact",
            done_when="done",
        ),
        capability={},
        estimated_context=100,
    )
    candidate_settings = {}

    def lane_gate(settings):
        candidate_settings[settings["model"]] = dict(settings)
        return {"state": "open"}

    with monkeypatch.context() as candidate_patches:
        candidate_patches.setattr(snapshot.routing, "_estimated_hours", lambda *a: 1)
        candidate_patches.setattr(
            snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
        )
        candidate_patches.setattr(
            snapshot.routing, "_context_fit_verdict", lambda **k: None
        )
        candidate_patches.setattr(snapshot, "_dispatch_lane_gate", lane_gate)
        candidate_patches.setattr(
            snapshot.resumption,
            "probe_lane_availability",
            lambda *a, **k: {"status": "served"},
        )
        offered = snapshot.candidates(
            request,
            config,
            tmp_path,
            records=[],
            verdict_inputs={},
            budget_snapshot={"backends": [], "groups": []},
        )
    candidates = {item.model: item for item in offered if item.family == "codex"}
    assert set(candidates) == {"gpt-6-sol", "gpt-6-astra", "gpt-6-luna"}
    assert set(candidate_settings) == set(candidates)
    for model, candidate in candidates.items():
        planned_settings = []
        real_plan = crew.plan_dispatch

        def capture_plan(
            *, _real_plan=real_plan, _planned_settings=planned_settings, **kwargs
        ):
            plan = _real_plan(**kwargs)
            _planned_settings.append(dict(plan.backend_settings))
            return plan

        with monkeypatch.context() as dispatch_patches:
            dispatch_patches.setattr(crew, "plan_dispatch", capture_plan)
            code, run = _dispatch(
                [
                    *_args(lane_repo),
                    "--route",
                    "deterministic",
                    "--backend",
                    "codex",
                    "--model",
                    model,
                ]
            )
        assert code == 0, run
        settings = candidate_settings[model]
        assert len(planned_settings) == 1
        assert planned_settings[0] == settings
        assert (
            run["agent"]["model"],
            run["agent"]["effort"],
            run["agent"]["sandbox"],
            run["time_budget"],
        ) == (
            candidate.model,
            candidate.effort,
            settings["sandbox"],
            settings["time_budget"],
        )
