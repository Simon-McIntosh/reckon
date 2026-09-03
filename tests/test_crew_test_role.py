"""Tests for the test role's own harness contract.

Covers the two measures the test role adds over an implementation role:

  - the shipped default declares a time budget for the role, resolved through
    the layered flight config with visible provenance
  - the composed prompt is role-aware in exactly two ways: a non-test role's
    evidence fence gains a division-of-labour sentence a test role's does not,
    and a test role's evidence fence gains an attribution deliverable a
    non-test role's does not
"""

from __future__ import annotations

import pytest

from reckon import crew
from reckon.flight import resolve


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep this file's flight resolution off the workstation's real host layer."""
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


# ── The test role resolves its declared budget through reckon flight ───────


def test_shipped_test_role_declares_its_own_time_budget(tmp_path):
    absent_host = tmp_path / "host" / "flight.yaml"
    absent_project = tmp_path / "project" / "flight.yaml"

    resolved = resolve(host_path=absent_host, project_path=absent_project)

    assert resolved.config["roles"]["test"]["time_budget"] == "30m"
    assert resolved.origin("roles.test.time_budget") == "shipped"


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
