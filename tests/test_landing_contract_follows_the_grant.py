"""The plan landing contract is stated only when the node's fence grants it.

The plan landing contract tells the worker to write its evidence anchor to the
node's own fragment and its figures to the node's own figure directory. Dispatch
grants those two paths to a role that lands work in the tree and withholds them
from a verifier, whose sandbox may still write the worktree but whose fence does
not carry the fragment. A contract composed on worktree writability alone would
tell that verifier to write a path its fence withholds, so the contract is
composed on both facts: the worker can write the worktree and the fragment is in
its write scope. A node whose fence withholds the fragment reads the run-record
carrier instead — the wording a brief already reads — so its landing line stays
on the run's own record.

These tests compose prompts through the dispatch path: a dry-run plan dispatch
resolves the node's write scope and its authority, and the same helper dispatch
uses to decide the contract is read against them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.dispatch import (
    _compose_dispatch_prompt,
    _writes_its_landing_fragment,
)
from reckon.crew.prompts import BRIEF_LANDING_CONTRACT, PLAN_LANDING_CONTRACT
from reckon.crew.runs import run_dir

PROJECT = "sample"
NODE_ID = "landing-grant-node"
PLAN_SECTION = "landing-grant"
FRAGMENT = f"docs/evidence/fragments/plan-a/{NODE_ID}.html"
FIGURE = f"docs/figures/plan-a/{NODE_ID}"

# The sentence the run-record carrier states in place of the plan one: the
# node's landing line stays on the run's own record rather than on a plan.
RUN_RECORD_SENTENCE = (
    "The `landing:` line lands on this run's own record, because a brief names "
    "no plan section for a promotion to land it on."
)

LANDING_FENCE_CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        "test": {"execution_capable": True, "sandbox": "worktree-full"},
        "implement": {"execution_capable": True, "sandbox": "worktree-full"},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


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
        "<!doctype html>"
        "<html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="plan-a">'
        '<meta name="plan-status" content="active">'
        f'<h2 id="{PLAN_SECTION}">Landing grant</h2>'
        "</head></html>"
    )


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A seeded repository whose mounted project carries a low-level plan."""
    root = tmp_path / "landing-repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    (root / "docs" / "plans" / "plan-a.html").write_text(_plan_html(), encoding="utf-8")
    (root / "package" / "out.py").write_text("RESULT = 'sound'\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "package"),
        (
            "commit",
            "-q",
            "-m",
            "chore: seed landing-grant repository",
            "-m",
            "Provide the plan and deliverable the landing contract resolves against.",
        ),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _node(home: Path, *, role: str, write_paths: list[str], manifest_path: str):
    return crew.TaskNode(
        id=NODE_ID,
        goal="check the landing contract follows the node's granted fragment",
        plan="plan-a",
        section=PLAN_SECTION,
        role=role,
        done_when="pytest tests/landing_grant.py reports 3 passed",
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=manifest_path,
        spec_level="exact",
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line-wrapping so a phrase can be found
    regardless of where the composer happened to break the line."""
    return " ".join(text.split())


def _composed_prompt(resolution, repository: Path, run_directory: Path) -> str:
    """Compose the prompt through the same call site dispatch composes from."""
    return _compose_dispatch_prompt(
        node=resolution.node,
        project=PROJECT,
        authority=resolution.authority,
        backend=resolution.backend_settings,
        repo_root=repository,
        run_directory=run_directory,
        worktree="/repo/worktrees/landing-grant",
        working_directory="/repo/worktrees/landing-grant",
        needs_help_after_failures=2,
    )


# ── A landing role reads the plan contract and its granted fragment ────────


def test_an_implement_node_reads_the_plan_contract_and_its_fragment(
    home: Path, repository: Path
):
    run_directory = run_dir("r-landing-grant-implement")
    resolution = crew.plan_dispatch(
        node=_node(
            home,
            role="implement",
            write_paths=["package/out.py"],
            manifest_path=str(run_directory / "manifest.md"),
        ),
        config=LANDING_FENCE_CONFIG,
        project=PROJECT,
        repo=repository,
    )
    assert resolution.validation.ok, resolution.validation.findings
    declared = list(resolution.node.write_paths)
    assert FRAGMENT in declared
    assert FIGURE in declared
    assert _writes_its_landing_fragment(resolution.node, authority=resolution.authority)

    prompt = _composed_prompt(resolution, repository, run_directory)
    assert PLAN_LANDING_CONTRACT in prompt
    assert FRAGMENT in prompt


# ── A writable verifier whose fence withholds the fragment reads neither ───


def test_a_writable_verifier_reads_the_run_record_carrier_not_the_plan_contract(
    home: Path, repository: Path
):
    run_directory = run_dir("r-landing-grant-verifier")
    resolution = crew.plan_dispatch(
        node=_node(
            home,
            role="test",
            write_paths=[str(run_directory)],
            manifest_path=str(run_directory / "manifest.md"),
        ),
        config=LANDING_FENCE_CONFIG,
        project=PROJECT,
        repo=repository,
    )
    assert resolution.validation.ok, resolution.validation.findings
    declared = list(resolution.node.write_paths)
    assert FRAGMENT not in declared
    assert FIGURE not in declared
    assert not _writes_its_landing_fragment(
        resolution.node, authority=resolution.authority
    )

    prompt = _composed_prompt(resolution, repository, run_directory)
    assert PLAN_LANDING_CONTRACT not in prompt
    assert FRAGMENT not in prompt
    assert FIGURE not in prompt
    assert _flat(RUN_RECORD_SENTENCE) in _flat(prompt)
    assert BRIEF_LANDING_CONTRACT in prompt


# ── The decision reads only the resolved scope, not the role ───────────────


def test_the_fragment_fact_reads_write_paths_not_the_role(home: Path):
    """A node whose scope carries the fragment reads the plan contract.

    The helper is proved to follow the scope rather than the role name: the
    same role reads either contract as its resolved write paths change, so the
    two cannot drift apart by a role list kept in a second place.
    """
    authority = {
        "plan": {
            "docs": "/repo/docs",
            "repository": "/repo",
        }
    }
    manifest = str(run_dir("r-landing-grant-fact") / "manifest.md")
    carry = _node(
        home,
        role="test",  # a verifier role, never a landing role
        write_paths=[FRAGMENT, FIGURE],
        manifest_path=manifest,
    )
    withhold = _node(
        home,
        role="test",
        write_paths=[str(run_dir("r-landing-grant-fact"))],
        manifest_path=manifest,
    )
    assert _writes_its_landing_fragment(carry, authority=authority)
    assert not _writes_its_landing_fragment(withhold, authority=authority)


def test_a_direct_composer_without_the_fact_keeps_the_plan_carrier():
    """A caller that resolves no scope keeps the carrier it has always read.

    The fact is a gate on top of worktree writability, not a replacement for
    it: a direct composer that supplies no scope keeps the plan carrier for a
    writable node, so the constant every direct composer already reads is
    unchanged.
    """
    node = crew.TaskNode(
        id=NODE_ID,
        goal="compose without the resolved fragment fact",
        plan="plan-a",
        section=PLAN_SECTION,
        role="implement",
        done_when="pytest tests/landing_grant.py reports 3 passed",
        write_paths=["package/out.py"],
        time_budget="20m",
        manifest_path="/state/runs/x/manifest.md",
        spec_level="exact",
    )
    prompt = crew.compose_prompt(
        node=node,
        project=PROJECT,
        worktree="/repo/worktrees/x",
        working_directory="/repo/worktrees/x",
        manifest_path="/state/runs/x/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        can_write_worktree=True,
    )
    assert PLAN_LANDING_CONTRACT in prompt
    assert _flat(RUN_RECORD_SENTENCE) not in _flat(prompt)
