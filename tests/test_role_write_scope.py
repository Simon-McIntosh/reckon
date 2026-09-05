"""Tests for refusing a role-scope mismatch at dispatch, not at promotion.

A verifier reads the repository it grades but writes only its own delivery —
manifest, report and logs — which live outside it. Dispatch validates a node
before any worktree exists, so a test-role node that declares repository source
paths is refused there instead of at promotion, where the worker time is already
spent. The dispatch validation and the promotion refusal share one predicate, so
a run that slipped through an earlier dispatch is caught later by the same rule
rather than by a copy that could drift from it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.node import role_may_write_repository_paths
from reckon.crew.runs import _write_json, pointer_path, reports_dir, run_dir

PROJECT = "sample"


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


def _node(
    home: Path,
    *,
    role: str,
    write_paths: list[str],
    manifest_path: str | None = None,
) -> crew.TaskNode:
    return crew.TaskNode(
        id="role-scope-node",
        goal="measure the timestamp work against a stated base",
        plan="plan-a",
        section="s1",
        role=role,
        done_when="pytest tests/example.py reports 3 passed",
        write_paths=write_paths,
        time_budget="20m",
        manifest_path=manifest_path or str(run_dir("r-role-scope-node") / "manifest.md"),
        spec_level="exact",
    )


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A seeded repository whose project is mounted under the temp config home."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    (root / "source.py").write_text("RESULT = 'sound'\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "source.py"),
        (
            "commit",
            "-q",
            "-m",
            "test: seed verification repository",
            "-m",
            "Provide the stated base for the synthetic verification wave.",
        ),
    ):
        _git(root, *arguments)
    return root


# ── The dispatch gate refuses the mismatch before any work exists ──────────


def test_a_test_role_declaring_repository_paths_is_refused_at_dispatch(home):
    verdict = crew.validate_node(
        _node(
            home,
            role="test",
            write_paths=[
                "pkg/standard_names/graph_ops.py",
                "pkg/schemas/standard_name.yaml",
            ],
        ),
        budget_ceiling="25m",
    )
    assert "scoped" in verdict.failed_properties
    detail = next(f["detail"] for f in verdict.findings if f["property"] == "scoped")
    assert "may not write repository paths" in detail
    assert "pkg/standard_names/graph_ops.py" in detail


def test_a_test_role_with_only_delivery_write_paths_still_dispatches(home):
    run_directory = run_dir("r-role-scope-delivery")
    verdict = crew.validate_node(
        _node(
            home,
            role="test",
            write_paths=[
                str(run_directory),
                str(reports_dir()),
                str(run_directory / "manifest.md"),
            ],
        ),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_a_test_role_declaring_its_own_manifest_elsewhere_still_dispatches(
    home, tmp_path
):
    manifest = tmp_path / "elsewhere" / "manifest.md"
    verdict = crew.validate_node(
        _node(
            home,
            role="test",
            write_paths=[str(manifest)],
            manifest_path=str(manifest),
        ),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_an_implement_role_declaring_repository_source_still_dispatches(home):
    verdict = crew.validate_node(
        _node(home, role="implement", write_paths=["reckon/crew/node.py"]),
        budget_ceiling="25m",
    )
    assert verdict.ok, verdict.findings


def test_dispatch_is_refused_before_a_worktree_exists(home):
    config = {
        "default_backend": "native",
        "backends": {"native": {"launch": "in-harness", "time_budget": "25m"}},
        "roles": {"test": {"execution_capable": True}, "implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    refused = crew.plan_dispatch(
        node=_node(home, role="test", write_paths=["source.py"]),
        config=config,
    )
    assert not refused.validation.ok
    assert "scoped" in refused.validation.failed_properties

    accepted = crew.plan_dispatch(
        node=_node(home, role="implement", write_paths=["source.py"]),
        config=config,
    )
    assert accepted.validation.ok, accepted.validation.findings


def test_a_dispatched_test_node_with_no_explicit_path_resolves_the_role_default(
    home,
):
    config = {
        "default_backend": "native",
        "backends": {"native": {"launch": "in-harness", "time_budget": "25m"}},
        "roles": {"test": {"execution_capable": True, "write_paths": ["."]}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    node = _node(home, role="test", write_paths=[])
    resolution = crew.plan_dispatch(node=node, config=config)

    assert resolution.validation.ok, resolution.validation.findings
    run_directory = crew.run_dir(resolution.run_id)
    for declared in resolution.node.write_paths:
        assert Path(declared).is_relative_to(run_directory)
    assert "scoped" not in resolution.validation.failed_properties


# ── The role predicate is the single spelling of the rule ──────────────────


def test_the_role_predicate_is_the_single_spelling_of_the_rule():
    assert not role_may_write_repository_paths("test")
    assert role_may_write_repository_paths("implement")
    assert role_may_write_repository_paths("")


def _write_test_pointer(
    *,
    run_id: str,
    repository: Path,
    base: str,
    manifest: Path,
    write_paths: list[str],
    role: str = "test",
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-04T10:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "merged-head-verification",
                "plan": "fixture",
                "section": "verification",
                "role": role,
                "time_budget": "30m",
                "write_paths": write_paths,
            },
        },
    )


def test_a_test_run_that_slipped_through_is_still_refused_at_promotion(
    repository,
    tmp_path,
):
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "source.py").write_text("RESULT = 'edited by verifier'\n")
    _git(repository, "add", "source.py")
    _git(
        repository,
        "commit",
        "-q",
        "-m",
        "test: change graded source",
        "-m",
        "Exercise the promotion boundary for a verifier-owned commit.",
    )
    commit = _git(repository, "rev-parse", "HEAD")
    manifest = tmp_path / "source-edit-manifest.md"
    manifest.write_text(
        "node: merged-head-verification\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: source.py\n"
        "tests: source-edit refusal exercised\n",
        encoding="utf-8",
    )
    run_id = "r-role-scope-slipped-through"
    _write_test_pointer(
        run_id=run_id,
        repository=repository,
        base=base,
        manifest=manifest,
        write_paths=["source.py"],
    )

    with pytest.raises(crew.CrewError, match="verifier may read"):
        crew.complete(
            run_id,
            gate="passed",
            commits=[commit],
            root=repository,
        )

    assert pointer_path(run_id).is_file()
