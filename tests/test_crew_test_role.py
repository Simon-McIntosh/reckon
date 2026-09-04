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

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.dispatch import _resolved_write_paths
from reckon.crew.promotion import _outside_declared_scope
from reckon.crew.runs import _write_json, pointer_path
from reckon.flight import resolve

PROJECT = "sample"
PRE_EXISTING_FAILURE = "tests/test_environment.py::test_external_service"
ADDED_FAILURE = "tests/test_regression.py::test_expected_result"


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


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_candidate(
    repository: Path,
    *,
    branch: str,
    path: str,
    content: str,
) -> str:
    _git(repository, "switch", "-q", "-c", branch)
    (repository / path).write_text(content, encoding="utf-8")
    _git(repository, "add", path)
    _git(
        repository,
        "commit",
        "-q",
        "-m",
        f"test: update {path}",
        "-m",
        "Build a candidate change for the synthetic verification wave.",
    )
    candidate = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "-q", "main")
    _git(
        repository,
        "merge",
        "-q",
        "--no-ff",
        branch,
        "-m",
        f"Merge {branch}",
        "-m",
        "Assemble the synthetic verification head.",
    )
    return candidate


@pytest.fixture()
def synthetic_wave(tmp_path: Path, home: Path) -> dict[str, object]:
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}),
        encoding="utf-8",
    )
    (repository / "source.py").write_text("RESULT = 'sound'\n", encoding="utf-8")
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
        _git(repository, *arguments)
    base = _git(repository, "rev-parse", "HEAD")
    clean_before = _commit_candidate(
        repository,
        branch="clean-observability",
        path="observability.txt",
        content="visible\n",
    )
    broken = _commit_candidate(
        repository,
        branch="introduces-regression",
        path="source.py",
        content="RESULT = 'broken'\n",
    )
    clean_after = _commit_candidate(
        repository,
        branch="clean-documentation",
        path="documentation.txt",
        content="explained\n",
    )
    return {
        "repository": repository,
        "base": base,
        "head": _git(repository, "rev-parse", "HEAD"),
        "candidates": [clean_before, broken, clean_after],
        "broken": broken,
        "home": home,
    }


def _suite_observation(revision: str, failures: list[str]) -> dict[str, object]:
    return {
        "revision": revision,
        "command": "pytest -q",
        "exit_status": 1 if failures else 0,
        "log_digest": f"sha256:{revision}",
        "completed": True,
        "failure_count": len(failures),
        "failure_ids": failures,
    }


def _write_attribution_manifest(
    path: Path,
    *,
    base: str,
    head: str,
    attribution: dict[str, str],
) -> None:
    baseline = _suite_observation(base, [PRE_EXISTING_FAILURE])
    after = _suite_observation(head, [PRE_EXISTING_FAILURE, ADDED_FAILURE])
    path.write_text(
        "\n".join(
            (
                "node: merged-head-verification",
                "status: complete",
                "commits: none",
                "changed_paths: none",
                "tests: synthetic paired suite observations complete",
                "baseline_suite: " + json.dumps(baseline),
                "after_suite: " + json.dumps(after),
                "failure_attribution: " + json.dumps(attribution),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _write_test_pointer(
    *,
    run_id: str,
    repository: Path,
    base: str,
    manifest: Path,
    write_paths: list[str],
    suite_command: str | None = None,
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
            "role": "test",
            "backend": "native",
            "created_at": "2026-09-04T10:00:00Z",
            "manifest_path": str(manifest),
            "suite_command": suite_command,
            "node": {
                "id": "merged-head-verification",
                "plan": "fixture",
                "section": "verification",
                "role": "test",
                "time_budget": "30m",
                "write_paths": write_paths,
            },
        },
    )


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


def test_a_test_node_commit_touching_a_source_path_is_refused_at_promotion(
    synthetic_wave,
    tmp_path,
):
    repository = synthetic_wave["repository"]
    base = synthetic_wave["head"]
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
    run_id = "r-test-source-edit"
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

    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).is_file()


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


def test_synthetic_wave_attribution_survives_in_the_promoted_ledger(
    synthetic_wave,
    tmp_path,
):
    repository = synthetic_wave["repository"]
    base = synthetic_wave["base"]
    head = synthetic_wave["head"]
    broken = synthetic_wave["broken"]
    candidates = synthetic_wave["candidates"]
    manifest = tmp_path / "attribution-manifest.md"
    _write_attribution_manifest(
        manifest,
        base=base,
        head=head,
        attribution={ADDED_FAILURE: broken},
    )
    run_id = "r-test-attributed-wave"
    _write_test_pointer(
        run_id=run_id,
        repository=repository,
        base=base,
        manifest=manifest,
        write_paths=[str(manifest.parent)],
        suite_command="pytest -q",
    )

    promoted = crew.complete(
        run_id,
        gate="passed",
        no_commit="report-only test role",
        suite_delta_waiver="retain the attributed synthetic regression",
        root=repository,
    )
    assert broken in candidates
    assert len(candidates) == 3
    manifest.unlink()

    stored = ledger.runs(PROJECT, root=repository)
    assert len(stored) == 1
    assert promoted["record"]["run_id"] == run_id
    suite_delta = stored[0]["suite_delta"]
    assert suite_delta["baseline_suite"]["failure_ids"] == [PRE_EXISTING_FAILURE]
    assert suite_delta["added_failure_ids"] == [ADDED_FAILURE]
    assert suite_delta["failure_attribution"] == {ADDED_FAILURE: broken}


def test_test_role_cannot_waive_missing_attribution_for_an_added_failure(
    synthetic_wave,
    tmp_path,
):
    repository = synthetic_wave["repository"]
    manifest = tmp_path / "missing-attribution-manifest.md"
    _write_attribution_manifest(
        manifest,
        base=synthetic_wave["base"],
        head=synthetic_wave["head"],
        attribution={},
    )
    run_id = "r-test-unattributed-wave"
    _write_test_pointer(
        run_id=run_id,
        repository=repository,
        base=synthetic_wave["base"],
        manifest=manifest,
        write_paths=[str(manifest.parent)],
        suite_command="pytest -q",
    )

    with pytest.raises(ledger.SuiteDeltaError, match="candidate commit"):
        crew.complete(
            run_id,
            gate="passed",
            no_commit="report-only test role",
            suite_delta_waiver="attempt to waive missing attribution",
            root=repository,
        )

    refusal = json.loads(pointer_path(run_id).read_text())["suite_delta_refusal"]
    assert refusal["missing_fields"] == [f"failure_attribution[{ADDED_FAILURE}]"]
    assert ledger.runs(PROJECT, root=repository) == []


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


DIVISION_SENTENCE = (
    "verifying the merged result belongs to a separately dispatched test node"
)
ATTRIBUTION_DELIVERABLE = "Your deliverable is an attribution, not a verdict"
REPORT_ONLY_BOUNDARY = "The repository is read-only for this role"


def test_implement_prompt_states_the_division_of_labour_not_attribution():
    prompt = _prompt("implement")

    assert DIVISION_SENTENCE in prompt
    assert ATTRIBUTION_DELIVERABLE not in prompt
    assert REPORT_ONLY_BOUNDARY not in prompt


def test_test_role_prompt_states_the_attribution_deliverable_not_division():
    prompt = _prompt("test")

    assert ATTRIBUTION_DELIVERABLE in prompt
    assert REPORT_ONLY_BOUNDARY in prompt
    assert DIVISION_SENTENCE not in prompt


def test_other_non_test_roles_also_get_the_division_sentence():
    prompt = _prompt("review")

    assert DIVISION_SENTENCE in prompt
    assert ATTRIBUTION_DELIVERABLE not in prompt
    assert REPORT_ONLY_BOUNDARY not in prompt
