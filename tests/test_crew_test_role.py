"""Tests for the test role's own harness contract.

Covers the three measures the test role adds over an implementation role:

  - the shipped default declares a time budget for the role, resolved through
    the layered flight config with visible provenance
  - the shipped default also declares the role's write scope — its own run's
    report-and-log directory, never the repository under test — resolved with
    the same visible provenance, filled in for a dispatched node that names no
    write path of its own, and refused at promotion the moment a commit
    reaches outside it
  - the composed prompt is role-aware in exactly two ways: a non-test role's
    evidence fence gains a division-of-labour sentence a test role's does not,
    and a test role's evidence fence gains an attribution deliverable a
    non-test role's does not
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.dispatch import _resolved_write_paths
from reckon.crew.promotion import _outside_declared_scope
from reckon.flight import resolve


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep this file's flight resolution off the workstation's real host layer."""
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


# ── The test role resolves its declared budget through reckon flight ───────


def test_shipped_test_role_declares_its_own_time_budget(tmp_path):
    absent_host = tmp_path / "host" / "flight.yaml"
    absent_project = tmp_path / "project" / "flight.yaml"

    resolved = resolve(host_path=absent_host, project_path=absent_project)

    assert resolved.config["roles"]["test"]["time_budget"] == "30m"
    assert resolved.origin("roles.test.time_budget") == "shipped"


# ── The test role resolves its declared write scope through reckon flight ──


def test_shipped_test_role_declares_its_own_write_scope(tmp_path):
    absent_host = tmp_path / "host" / "flight.yaml"
    absent_project = tmp_path / "project" / "flight.yaml"

    resolved = resolve(host_path=absent_host, project_path=absent_project)

    assert resolved.config["roles"]["test"]["write_paths"] == ["."]
    assert resolved.origin("roles.test.write_paths") == "shipped"


def test_role_write_scope_resolves_against_the_run_directory_not_a_repository(
    tmp_path,
):
    run_directory = tmp_path / "crew" / "runs" / "r-test-run"

    resolved = _resolved_write_paths(
        {"write_paths": ["."]}, run_directory=run_directory
    )

    assert resolved == [str(run_directory.resolve())]
    assert all(not Path(path).is_relative_to(Path.cwd()) for path in resolved), (
        "a role's default write path must never resolve inside the repository"
    )


def test_a_role_declaring_no_write_paths_resolves_to_nothing():
    assert _resolved_write_paths({}, run_directory=Path("/anywhere")) == []


def test_a_dispatched_test_node_with_no_explicit_write_path_resolves_the_role_default(
    home,
):
    config = {
        "default_backend": "native",
        "backends": {"native": {"launch": "in-harness", "time_budget": "25m"}},
        "roles": {"test": {"execution_capable": True, "write_paths": ["."]}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    node = crew.TaskNode(
        id="test-node",
        goal="attribute suite failures against a stated base",
        plan="plan-a",
        section="s7",
        role="test",
        done_when="the attribution names a candidate commit for each new test failure",
        write_paths=[],
        time_budget="20m",
    )

    resolution = crew.plan_dispatch(node=node, config=config)

    assert resolution.node.write_paths, (
        "no write path was resolved from the role default"
    )
    run_directory = crew.run_dir(resolution.run_id)
    for declared in resolution.node.write_paths:
        assert Path(declared).is_relative_to(run_directory)
    assert "scoped" not in resolution.validation.failed_properties


def test_a_test_node_commit_touching_a_source_path_is_refused_at_promotion(tmp_path):
    run_directory = tmp_path / "crew" / "runs" / "r-test-run"
    declared = _resolved_write_paths(
        {"write_paths": ["."]}, run_directory=run_directory
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    outside = _outside_declared_scope(
        ["reckon/crew/dispatch.py"],
        declared,
        record={"repo": str(repo)},
        tree=repo,
    )

    assert outside == ("reckon/crew/dispatch.py",)


def test_a_test_node_report_only_commit_is_not_refused_at_promotion(tmp_path):
    run_directory = tmp_path / "crew" / "runs" / "r-test-run"
    declared = _resolved_write_paths(
        {"write_paths": ["."]}, run_directory=run_directory
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    outside = _outside_declared_scope(
        [],
        declared,
        record={"repo": str(repo)},
        tree=repo,
    )

    assert outside == ()


# ── The composed prompt is role-aware in exactly two ways ──────────────────


def _node(*, role: str) -> crew.TaskNode:
    return crew.TaskNode(
        id="role-node",
        goal="check role-aware prompt composition",
        plan="plan-a",
        section="s7",
        role=role,
        done_when="the composed prompt names the right measure for this role",
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )


def _prompt(role: str) -> str:
    return crew.compose_prompt(
        node=_node(role=role),
        project="proj",
        worktree="/repo/worktrees/role-run",
        working_directory="/repo/worktrees/role-run",
        manifest_path="/state/runs/role-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


DIVISION_SENTENCE = "verifying the merged result belongs to a separately dispatched test node"
ATTRIBUTION_DELIVERABLE = "Your deliverable is an attribution, not a verdict"


def test_implement_prompt_states_the_division_of_labour_not_attribution():
    prompt = _prompt("implement")

    assert DIVISION_SENTENCE in prompt
    assert ATTRIBUTION_DELIVERABLE not in prompt


def test_test_role_prompt_states_the_attribution_deliverable_not_division():
    prompt = _prompt("test")

    assert ATTRIBUTION_DELIVERABLE in prompt
    assert DIVISION_SENTENCE not in prompt


def test_other_non_test_roles_also_get_the_division_sentence():
    prompt = _prompt("review")

    assert DIVISION_SENTENCE in prompt
    assert ATTRIBUTION_DELIVERABLE not in prompt
