"""A commitless manifest promotes as delivered, and each role is told how.

A run whose recorded role is investigate or review has no commit to name, so its
manifest states ``commits: none`` — bare, or with a reason in parentheses after
it, the shape the four refused runs wrote: ``commits: none (read-only sandbox;
nothing committed)``. Four such runs — two investigations and two plan reviews —
were refused with a refusal that the identifier "does not resolve to a commit",
and each manifest was blanked by hand before it could promote.

The declaration is not a citation: the field says the run committed nothing, and
the run record carries the base revision it started from. The guard that must
stay is the one that refuses a run stating ``none`` while its worktree head is
ahead of that base — there the run did commit, and a manifest denying it is the
record discarding the work.

The fixture runs a real repository and a real worktree for the run under the
shared test isolation, and asserts afterwards that the workstation's real
pointer directory is untouched, because an isolated read does not prove an
isolated write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from reckon.crew.reports import audit_manifest
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "commitless-manifest-fixture"
PLAN = "commitless-manifest-target"

# The line the refused runs delivered, verbatim, and the bare form.
STATED_WITH_REASON = "none (read-only sandbox; nothing committed)"
STATED_BARE = "none"

REASON = "the report is the deliverable and lives outside the repository"

INVESTIGATE_NODE = "investigate-commitless-manifest"
PLAN_REVIEW_NODE = f"plan-review-of-{PLAN}"

RUN_IDS = (
    "r-20261115T100000000001-investigate-commitless",
    "r-20261115T100000000002-plan-review-commitless",
    "r-20261115T100000000003-investigate-ahead-of-base",
    "r-20261115T100000000004-investigate-bare-none",
    "r-20261115T100000000005-plan-review-bare-none",
)


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Commitless manifest target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(home: Path) -> Path:
    """A real repository, with its mount registered under the test's home."""
    root = home.parent / "repo"
    _write_plan(root)
    (root / "candidate.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "candidate.txt"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def base_sha(repository: Path) -> str:
    return _git(repository, "rev-parse", "HEAD")


def _worktree(repository: Path, home: Path, base_sha: str) -> Path:
    tree = home.parent / "worktrees" / "run"
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", "--quiet", str(tree), base_sha)
    return tree


def _commitless_run(
    repository: Path,
    home: Path,
    tree: Path,
    *,
    run_id: str,
    role: str,
    node_id: str,
    base_sha: str,
    commits: str,
) -> Path:
    manifest = home.parent / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {node_id}\n"
        "status: complete\n"
        f"commits: {commits}\n"
        f"changed_paths: {home.parent / 'crew' / 'reviews' / f'{run_id}.json'}\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": base_sha,
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-11-15T10:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": node_id,
                "plan": "",
                "section": "commitless",
                "time_budget": "25m",
                "role": role,
                "write_paths": ["candidate.txt"],
            },
        },
    )
    return manifest


def _node(node_id: str, role: str) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal="deliver the report",
        plan="",
        section="commitless",
        role=role,
        done_when="the report is delivered",
        write_paths=["candidate.txt"],
        time_budget="25m",
    )


@pytest.mark.parametrize(
    ("run_id", "role", "node_id", "commits"),
    [
        pytest.param(
            RUN_IDS[0],
            "investigate",
            INVESTIGATE_NODE,
            STATED_WITH_REASON,
            id="investigate-with-a-reason",
        ),
        pytest.param(
            RUN_IDS[1],
            "review",
            PLAN_REVIEW_NODE,
            STATED_WITH_REASON,
            id="plan-review-with-a-reason",
        ),
        pytest.param(
            RUN_IDS[3],
            "investigate",
            INVESTIGATE_NODE,
            STATED_BARE,
            id="investigate-bare-none",
        ),
        pytest.param(
            RUN_IDS[4],
            "review",
            PLAN_REVIEW_NODE,
            STATED_BARE,
            id="plan-review-bare-none",
        ),
    ],
)
def test_a_commitless_manifest_passes_the_check_and_promotes(
    repository: Path,
    home: Path,
    base_sha: str,
    run_id: str,
    role: str,
    node_id: str,
    commits: str,
) -> None:
    """The declaration passes the write-time check and promotes with no commit."""
    tree = _worktree(repository, home, base_sha)
    manifest = _commitless_run(
        repository,
        home,
        tree,
        run_id=run_id,
        role=role,
        node_id=node_id,
        base_sha=base_sha,
        commits=commits,
    )

    audit = audit_manifest(
        manifest.read_text(encoding="utf-8"),
        _node(node_id, role),
        worktree=tree,
        repository=repository,
        project=PROJECT,
        manifest_path=manifest,
    )
    assert "status is complete but no commit is recorded" not in audit["findings"]

    promoted = crew.complete(run_id, gate="passed", root=repository, no_commit=REASON)

    assert promoted["record"]["commits"] == []
    assert promoted["record"]["base_sha"] == base_sha
    assert not pointer_path(run_id).exists()


def test_a_none_declaration_over_a_commit_beyond_the_base_is_refused(
    repository: Path,
    home: Path,
    base_sha: str,
) -> None:
    """A run that did commit and states none is refused, naming the commit."""
    tree = _worktree(repository, home, base_sha)
    (tree / "candidate.txt").write_text("changed\n", encoding="utf-8")
    _git(tree, "add", "candidate.txt")
    _git(tree, "commit", "-q", "-m", "change: edit the candidate")
    head = _git(tree, "rev-parse", "HEAD")

    run_id = RUN_IDS[2]
    _commitless_run(
        repository,
        home,
        tree,
        run_id=run_id,
        role="investigate",
        node_id=INVESTIGATE_NODE,
        base_sha=base_sha,
        commits=STATED_WITH_REASON,
    )

    with pytest.raises(crew.CrewError, match=head[:12]):
        crew.complete(run_id, gate="passed", root=repository, no_commit=REASON)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def _prompt(role: str) -> str:
    return compose_prompt(
        node=_node("commitless-manifest-node", role),
        project=PROJECT,
        worktree="/repo/worktrees/commitless-run",
        working_directory="/repo/worktrees/commitless-run",
        manifest_path="/state/runs/commitless-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


@pytest.mark.parametrize("role", ["investigate", "review"])
def test_the_prompt_for_a_commitless_role_states_the_none_line(role: str) -> None:
    """The composed prompt tells the run how to state it committed nothing."""
    prompt = " ".join(_prompt(role).split())

    assert "commits: none" in prompt


def test_the_prompt_for_a_committing_role_keeps_the_sha_line() -> None:
    prompt = " ".join(_prompt("implement").split())

    assert "commits: <sha list>" in prompt
