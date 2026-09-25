from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, flight
from reckon.crew import plan_review

_CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        "implement": {
            "backend": "worker",
            "execution_capable": True,
        }
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _store_answered_review(plan_path: Path, *, project: str, slug: str) -> None:
    """Give the fixture plan the answered review a build dispatch now requires.

    A build dispatch is refused until the plan carries a review of its current
    content, so a dispatch fixture must first clear this precondition. The
    record is stored at the store root the gate reads through, resolved from
    RECKON_HOME, and joined to the plan by its content fingerprint.
    """
    plan_review.store_plan_review(
        {
            "project": project,
            "plan_slug": slug,
            "plan_version": 1,
            "rubric": "plan_review",
            "reviewed_blob_sha": "a" * 40,
            "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
            "findings": [
                {
                    "id": "stated-cause",
                    "type": "reasoning",
                    "text": "the stated cause matches the cited evidence",
                }
            ],
            "responses": {
                "stated-cause": {
                    "action": "declined",
                    "reason": "the fixture plan asserts no cause, so none is unproven",
                }
            },
            "status": "declined",
            "review_run_id": "r-plan-review-fixture",
        }
    )


def _repository(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
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
    plan_path = plans / "routing.html"
    plan_path.write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="routing">'
        '<h2 id="dispatch">Dispatch routing</h2>',
        encoding="utf-8",
    )
    _store_answered_review(plan_path, project="sample", slug="routing")
    (repo / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "skills", "target.py"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _node() -> crew.TaskNode:
    return crew.TaskNode(
        id="routing-visibility",
        goal="report the worker configuration before launch",
        plan="routing",
        section="dispatch",
        role="implement",
        spec_level="guided",
        done_when="pytest reports worker configuration parity with zero failures",
        write_paths=["target.py"],
        time_budget="20m",
    )


def test_dry_run_agent_equals_successful_dispatch_agent(tmp_path, monkeypatch) -> None:
    repo = _repository(tmp_path, monkeypatch)
    definition = _node()
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *args, **kwargs: _CONFIG)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            "sample",
            "--plan",
            definition.plan,
            "--section",
            definition.section,
            "--role",
            definition.role,
            "--spec-level",
            definition.spec_level,
            "--node",
            definition.id,
            "--goal",
            definition.goal,
            "--done-when",
            definition.done_when,
            "--write-path",
            definition.write_paths[0],
            "--time-budget",
            definition.time_budget,
            "--session",
            "routing-visibility",
            "--repo",
            str(repo),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    dry_run = json.loads(result.output)
    launched = crew.dispatch(
        node=_node(),
        project="sample",
        repo=repo,
        config=_CONFIG,
        session="routing-visibility",
        check_budget=False,
    )

    assert dry_run["agent"] == launched["agent"]
    assert dry_run["agent"] == {
        "backend": "worker",
        "launch": "in-harness",
        "model": "test-model",
        "effort": "high",
        "sandbox": "worktree-full",
    }


def test_level_effort_overlays_an_explicit_backend_effort(tmp_path) -> None:
    shipped = tmp_path / "flight.yaml"
    shipped.write_text(
        """
default_backend: codex
backends:
  codex:
    launch: in-harness
    model: test-model
    effort: low
    sandbox: worktree-full
roles:
  implement:
    backend: codex
    by_spec_level:
      guided:
        effort: medium
fences:
  time_budget: 20m
  needs_help_after_failures: 2
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = flight.resolve(
        shipped_path=shipped,
        host_path=tmp_path / "absent-host.yaml",
        overrides=flight.parse_overrides(["backends.codex.effort=high"]),
    ).config

    backend_name, resolved = crew.resolve_role(config, "implement", "guided")

    assert config["backends"]["codex"]["effort"] == "high"
    assert backend_name == "codex"
    assert resolved["effort"] == "medium"
    help_result = CliRunner().invoke(cli_module.main, ["crew", "dispatch", "--help"])
    assert help_result.exit_code == 0
    assert "roles.<role>.by_spec_level.<level>.effort" in help_result.output
    assert "overlays backends.<name>.effort" in help_result.output
