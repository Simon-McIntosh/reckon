"""The landing contract and the promotion gate decide by one criterion.

A dispatch that hands a worker the landing contract instructs it to land a
record and, for a role that may write repository paths, to commit them. The
promotion gate refuses a commit from a role that may not write repository
paths. The two surfaces therefore have to read one predicate for "this role may
land repository paths": composing the contract on sandbox writability alone
gives a writable verifier an instruction its own promotion refuses.

These cases compose prompts through the dispatch path with the shipped flight
defaults, so the roles, sandboxes and write scopes under test are the ones a
real dispatch resolves rather than a copy kept here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon._backends import READ_ONLY
from reckon.crew.dispatch import _can_write_worktree, _compose_dispatch_prompt
from reckon.crew.node import role_may_write_repository_paths
from reckon.crew.prompts import (
    BRIEF_LANDING_CONTRACT,
    PLAN_LANDING_CONTRACT,
)
from reckon.crew.runs import run_dir
from reckon.flight import resolve

PROJECT = "sample"
PLAN_SECTION = "landing-gate"


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep flight resolution off the workstation's real host layer."""
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew directory at a temp tree, leaving the real one alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _plan_html() -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<meta name="plan-status" content="active">'
        f'<h2 id="{PLAN_SECTION}">Landing gate</h2>'
        "</head></html>"
    )


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "landing-gate-repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    (root / "docs" / "plans" / "plan-a.html").write_text(_plan_html(), encoding="utf-8")
    (root / "package" / "out.py").write_text("RESULT = 1\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "package"),
        (
            "commit",
            "-q",
            "-m",
            "chore: seed landing gate repository",
            "-m",
            "Provide the plan and deliverable the contract resolves against.",
        ),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _shipped_config(tmp_path: Path) -> dict:
    absent = tmp_path / "absent-flight.yaml"
    return resolve(host_path=absent, project_path=absent).config


def _node(role: str, write_paths: list[str], manifest_path: str):
    executable = role not in {"review", "investigate"}
    return crew.TaskNode(
        id=f"agree-{role}",
        goal=f"hold the landing contract and the promotion gate to one criterion ({role})",
        plan="plan-a",
        section=PLAN_SECTION,
        role=role,
        done_when=(
            "pytest tests/agree.py reports 3 passed"
            if executable
            else "the report lists each role whose contract and gate disagree with the command output that shows it"
        ),
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=manifest_path,
        spec_level="exact",
    )


def _landing_contract_in(prompt: str) -> bool:
    return PLAN_LANDING_CONTRACT in prompt or BRIEF_LANDING_CONTRACT in prompt


# ── Every role in the shipped flight configuration ─────────────────────────


def test_every_default_role_agrees_with_the_promotion_gate(home, tmp_path, repository):
    """For each shipped role, the contract tracks the gate's own predicate.

    A role is given the landing contract exactly when it may land repository
    paths and its sandbox can write the assigned worktree. A writable verifier
    therefore reads no contract rather than one its promotion refuses, and the
    read-only roles read none because they cannot commit anything.
    """
    config = _shipped_config(tmp_path)
    disagreements: list[str] = []
    for role in sorted(config["roles"]):
        run_directory = run_dir(f"r-agree-{role}")
        # A role whose sandbox cannot write the worktree, or that may not write
        # repository paths, declares its own run directory, as its delivery
        # does; dispatch refuses a repository path it could never reach.
        reaches_repository = (
            role_may_write_repository_paths(role)
            and config["roles"][role].get("sandbox") != READ_ONLY
        )
        write_paths = ["package/out.py"] if reaches_repository else [str(run_directory)]
        resolution = crew.plan_dispatch(
            node=_node(role, write_paths, str(run_directory / "manifest.md")),
            config=config,
            project=PROJECT,
            repo=repository,
        )
        assert resolution.validation.ok, (role, resolution.validation.findings)
        writable = _can_write_worktree(
            resolution.backend_settings,
            repository=repository,
            run_directory=run_directory,
        )
        expected = writable and role_may_write_repository_paths(role)
        prompt = _compose_dispatch_prompt(
            node=resolution.node,
            project=PROJECT,
            authority=resolution.authority,
            backend=resolution.backend_settings,
            repo_root=repository,
            run_directory=run_directory,
            worktree="/repo/worktrees/agree",
            working_directory="/repo/worktrees/agree",
            needs_help_after_failures=2,
        )
        if _landing_contract_in(prompt) != expected:
            disagreements.append(
                f"{role}: contract={_landing_contract_in(prompt)} expected={expected}"
            )
    assert not disagreements, disagreements


# ── The test role with an out-of-repository report path ────────────────────


def test_a_test_role_with_an_out_of_repository_report_path_gains_no_landing_paths(
    home, tmp_path, repository
):
    """The verifier's declared scope is its report path, and nothing else.

    A test-role dispatch declares an out-of-repository report path. The
    contract that lands a record in the repository, and the plan, evidence and
    figure paths that go with it, are granted to roles that land repository
    paths; this role's record stays on the run's own record and its prompt
    carries no landing contract to follow.
    """
    config = _shipped_config(tmp_path)
    run_directory = run_dir("r-agree-test-report")
    resolution = crew.plan_dispatch(
        node=_node(
            "test",
            [str(run_directory)],
            str(run_directory / "manifest.md"),
        ),
        config=config,
        project=PROJECT,
        repo=repository,
    )
    assert resolution.validation.ok, resolution.validation.findings
    granted = [str(path) for path in resolution.node.write_paths]
    assert str(run_directory) in granted
    for marker in ("docs/plans/", "docs/evidence/", "docs/figures/"):
        assert not any(marker in path for path in granted), granted

    prompt = _compose_dispatch_prompt(
        node=resolution.node,
        project=PROJECT,
        authority=resolution.authority,
        backend=resolution.backend_settings,
        repo_root=repository,
        run_directory=run_directory,
        worktree="/repo/worktrees/agree",
        working_directory="/repo/worktrees/agree",
        needs_help_after_failures=2,
    )
    assert not _landing_contract_in(prompt)
    assert not role_may_write_repository_paths(resolution.node.role)
