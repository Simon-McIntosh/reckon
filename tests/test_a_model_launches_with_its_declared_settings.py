"""A lane model launches at the effort and time budget it declares."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from reckon import cli, crew, flight
from reckon.crew.node import TaskNode
from reckon.crew.picker import PickRequest, snapshot

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
      luna: {model: gpt-6-luna, effort: xhigh}
  claude:
    launch: cli
    command: claude
    sandbox: worktree-full
    time_budget: 20m
    default_model: sonnet
    models:
      sonnet: {model: claude-sonnet-5-5, effort: medium}
backends:
  clive:
    launch: cli
    command: clive
    sandbox: worktree-full
    effort: xhigh
    time_budget: 25m
roles:
  implement:
    execution_capable: true
    time_budget: 8m
    by_spec_level:
      exact: {effort: high}
fences:
  time_budget: 60m
  needs_help_after_failures: 2
"""

# Every model the fixture declares, with the settings it should launch at once
# its own declaration outranks the role overlay. The codex lane lights the
# three cases the role overlay would otherwise flatten to high and 8m; the
# claude lane's sonnet is declared at medium; clive is no lane model at all, so
# it keeps the overlay's values.
DECLARED_MODELS = (
    ("codex", "sol", "gpt-6-sol", "high", "8m"),
    ("codex", "astra", "gpt-6-astra", "xhigh", "55m"),
    ("codex", "luna", "gpt-6-luna", "xhigh", "8m"),
    ("claude", "sonnet", "claude-sonnet-5-5", "medium", "8m"),
)

CATALOGUE = {
    "version": 1,
    "lanes": {
        "codex": {
            "default_model": "sol",
            "models": {
                key: {"model": f"gpt-6-{key}"} for key in ("sol", "astra", "luna")
            },
        },
        "claude": {
            "default_model": "sonnet",
            "models": {"sonnet": {"model": "claude-sonnet-5-5"}},
        },
    },
}


@pytest.fixture
def declared_repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    catalogue_path = tmp_path / "catalogue.yaml"
    catalogue_path.write_text(yaml.safe_dump(CATALOGUE))
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
        "declared-settings",
        "--goal",
        "launch a model at the settings it declares",
        "--done-when",
        "pytest tests/test_a_model_launches_with_its_declared_settings.py passes",
        "--write-path",
        "target.py",
        "--session",
        "declared-settings-session",
        "--repo",
        str(repo),
        "--no-watch",
        "--dry-run",
    ]


def _dispatch(
    repo: Path, extra: list[str]
) -> tuple[int, dict, list[dict]]:
    """Dispatch through the CLI, capturing the settings the plan resolved.

    ``plan.backend_settings`` is the launched settings the dispatch path
    resolved, so a case can assert on it directly rather than on a second
    implementation of the same resolution.
    """
    planned: list[dict] = []
    real_plan = crew.plan_dispatch

    def capture_plan(*, _real_plan=real_plan, _planned=planned, **kwargs):
        plan = _real_plan(**kwargs)
        _planned.append(dict(plan.backend_settings))
        return plan

    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(crew, "plan_dispatch", capture_plan)
        result = CliRunner().invoke(cli.main, [*_args(repo), *extra])
    run = json.loads(result.output.splitlines()[0])
    return result.exit_code, run, planned


@pytest.mark.parametrize(
    ("lane", "key", "model", "effort", "budget"), DECLARED_MODELS
)
def test_lane_model_launches_at_its_declared_settings(
    declared_repo: Path, lane: str, key: str, model: str, effort: str, budget: str
) -> None:
    code, run, planned = _dispatch(
        declared_repo,
        ["--route", "deterministic", "--backend", lane, "--model", key],
    )
    assert code == 0, run
    assert run["backend"] == lane
    assert run["agent"]["model"] == model
    assert run["agent"]["effort"] == effort
    assert run["time_budget"] == budget
    assert len(planned) == 1
    assert planned[0]["effort"] == effort
    assert planned[0]["time_budget"] == budget
    assert planned[0].get("model_key") == key


def test_non_lane_backend_keeps_the_overlay_effort(declared_repo: Path) -> None:
    code, run, planned = _dispatch(
        declared_repo, ["--route", "deterministic", "--backend", "clive"]
    )
    assert code == 0, run
    assert run["backend"] == "clive"
    assert run["agent"]["effort"] == "high"
    assert run["time_budget"] == "8m"
    assert planned[0].get("model_key") is None


def test_explicit_time_budget_wins_on_a_lane_model(declared_repo: Path) -> None:
    code, run, _ = _dispatch(
        declared_repo,
        [
            "--route",
            "deterministic",
            "--backend",
            "codex",
            "--model",
            "astra",
            "--time-budget",
            "33m",
        ],
    )
    assert code == 0, run
    assert run["time_budget"] == "33m"
    assert run["agent"]["effort"] == "xhigh"


def test_explicit_effort_wins_on_a_lane_model(declared_repo: Path) -> None:
    code, run, _ = _dispatch(
        declared_repo,
        [
            "--route",
            "deterministic",
            "--backend",
            "codex",
            "--model",
            "astra",
            "--set",
            "lanes.codex.models.astra.effort=low",
        ],
    )
    assert code == 0, run
    assert run["agent"]["effort"] == "low"
    assert run["time_budget"] == "55m"


def test_candidate_rows_show_what_the_dispatch_launches(
    declared_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = flight.resolve("sample", checkout_path=declared_repo).config
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
    rows: dict[str, dict] = {}

    def lane_gate(settings):
        rows[settings.get("model")] = dict(settings)
        return {"state": "open"}

    with monkeypatch.context() as patches:
        patches.setattr(snapshot.routing, "_estimated_hours", lambda *a: 1)
        patches.setattr(
            snapshot.routing, "_competence_verdict", lambda **k: {"allowed": True}
        )
        patches.setattr(snapshot.routing, "_context_fit_verdict", lambda **k: None)
        patches.setattr(snapshot, "_dispatch_lane_gate", lane_gate)
        patches.setattr(
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
    assert offered
    for row in offered:
        assert row.model in rows, row.model
        assert row.effort == rows[row.model].get("effort")
    for lane, key, model, effort, budget in DECLARED_MODELS:
        code, _run, planned = _dispatch(
            declared_repo,
            ["--route", "deterministic", "--backend", lane, "--model", key],
        )
        assert code == 0
        resolved = planned[0]
        assert rows[model].get("model_key") == resolved.get("model_key")
        assert (
            rows[model]["effort"],
            rows[model]["time_budget"],
        ) == (
            resolved["effort"],
            resolved["time_budget"],
        )
        assert (rows[model]["effort"], rows[model]["time_budget"]) == (effort, budget)