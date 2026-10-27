"""An armed project's suite command is scoped to the node's own test files.

The promotion-consequence gate runs a node's suite twice, at its base and at
its head, so arming it with the whole suite costs most of a node's budget. The
declaration may therefore be scoped with ``{node_test_files}``, which stands
for the test files among the node's fenced write paths. Promotion resolves it
and measures the pair against exactly that command: a pair that ran other
files, or the whole suite, measured a different selection and is refused.

A project whose declaration is removed is unarmed, and promotion records no
suite observation at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import flight
from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.promotion_records import _node_test_files, _scoped_suite_command
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"
SCOPED = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q {node_test_files}"
)
RESOLVED = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q "
    "tests/test_one.py tests/test_two.py"
)
NODE_WRITE_PATHS = [
    "reckon/crew/promotion_records.py",
    "tests/test_one.py",
    "tests/test_two.py",
    "docs/evidence/fragments/plan-a/node-a.html",
    "docs/figures/plan-a/node-a",
]


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<meta name="plan-effort-hours" content="4">'
        f"<title>{PLAN}</title></head><body></body></html>"
    )
    _seed_git_repository(root)
    return root


def _seed_git_repository(root: Path) -> None:
    """Make the fixture a git worktree with one commit."""
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if staged.returncode != 0:
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed repository"],
            cwd=root,
            check=True,
            capture_output=True,
        )


def _write_pointer(
    run_id: str,
    repository: Path,
    *,
    suite_command: str | None,
    manifest_path: str,
    write_paths: list[str] | None = None,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-08-26T09:00:00Z",
            "manifest_path": manifest_path,
            "base_sha": "base-abc",
            "suite_command": suite_command,
            "worktree": str(repository),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "20m",
                "write_paths": NODE_WRITE_PATHS if write_paths is None else write_paths,
            },
        },
    )


def _suite_observation(command: str, revision: str) -> dict[str, object]:
    return {
        "revision": revision,
        "command": command,
        "exit_status": 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }


def _write_manifest(path: Path, arms: tuple[dict[str, object], ...] = ()) -> None:
    lines = ["node: node-a", "status: complete", "commits: abc123", "tests: done"]
    if len(arms) == 2:
        lines.append("baseline_suite: " + json.dumps(arms[0]))
        lines.append("after_suite: " + json.dumps(arms[1]))
    path.write_text("\n".join(lines) + "\n")


def _stored_review(run_id: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(record)


def _complete(run_id: str, repository: Path):
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--checkout-path",
            str(repository),
            "--gate-command",
            "pytest -q tests/",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/gate.log",
            "--no-commit",
            "report-only fixture",
        ],
    )


def test_the_scope_resolves_to_only_the_test_files_it_holds() -> None:
    """Source, evidence and figure paths in the scope are not tests."""
    record = {
        "worktree": "/repo",
        "node": {"write_paths": NODE_WRITE_PATHS},
    }
    assert _node_test_files(record) == ["tests/test_one.py", "tests/test_two.py"]


def test_an_absolute_scope_path_inside_the_worktree_is_named_relatively() -> None:
    """Both arms run against different trees, so the command must carry no tree."""
    record = {
        "worktree": "/repo",
        "node": {
            "write_paths": [
                "/repo/tests/test_one.py",
                "/elsewhere/run-dir/manifest.md",
            ]
        },
    }
    assert _node_test_files(record) == ["tests/test_one.py"]


def _declared_suite_command() -> str:
    """The suite command this project's flight file arms the gate with."""
    root = Path(__file__).resolve().parents[1]
    layer = flight.read_layer_file(
        flight.project_config_path("reckon", checkout_path=root)
    )
    gates = layer.get("gates")
    return str((gates or {}).get("suite_command") or "").strip()


def test_the_project_arms_the_gate_with_a_scoped_command() -> None:
    """The declaration names the node's own tests, not the whole suite."""
    declared = _declared_suite_command()
    assert "{node_test_files}" in declared
    assert declared.rstrip().split()[-1] == "{node_test_files}"


def test_the_declared_command_measures_the_nodes_fenced_test_files(
    repository: Path, tmp_path: Path
) -> None:
    """A pair run with the declared command, resolved, is what promotes."""
    declared = _declared_suite_command()
    resolved = declared.replace(
        "{node_test_files}", "tests/test_one.py tests/test_two.py"
    )
    run_id = "r-20261027T010050000000-node-a"
    manifest = tmp_path / "declared.md"
    _write_manifest(
        manifest,
        arms=(
            _suite_observation(resolved, "base-abc"),
            _suite_observation(resolved, "after-abc"),
        ),
    )
    _write_pointer(
        run_id, repository, suite_command=declared, manifest_path=str(manifest)
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["suite_command"] == resolved
    assert suite_delta["after_suite"]["command"] == resolved


def test_the_declaration_resolves_to_the_nodes_fenced_test_files() -> None:
    record = {
        "suite_command": SCOPED,
        "worktree": "/repo",
        "node": {"write_paths": NODE_WRITE_PATHS},
    }
    assert _scoped_suite_command(record) == RESOLVED


def test_an_unscoped_declaration_is_taken_as_written() -> None:
    record = {
        "suite_command": "pytest -q tests/",
        "worktree": "/repo",
        "node": {"write_paths": NODE_WRITE_PATHS},
    }
    assert _scoped_suite_command(record) == "pytest -q tests/"


def test_a_scope_holding_no_test_file_measures_nothing() -> None:
    record = {
        "suite_command": SCOPED,
        "worktree": "/repo",
        "node": {"write_paths": ["reckon/crew/promotion_records.py"]},
    }
    assert _scoped_suite_command(record) == ""


def test_a_scoped_pair_over_the_nodes_test_files_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """The pair ran exactly the resolved command, so it measures what was armed."""
    run_id = "r-20261027T010000000000-node-a"
    manifest = tmp_path / "scoped.md"
    _write_manifest(
        manifest,
        arms=(
            _suite_observation(RESOLVED, "base-abc"),
            _suite_observation(RESOLVED, "after-abc"),
        ),
    )
    _write_pointer(
        run_id, repository, suite_command=SCOPED, manifest_path=str(manifest)
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "clean"
    assert suite_delta["suite_command"] == RESOLVED
    assert suite_delta["added_failure_ids"] == []
    assert suite_delta["baseline_suite"]["command"] == RESOLVED
    assert suite_delta["after_suite"]["command"] == RESOLVED


def test_a_scoped_pair_over_the_whole_suite_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The whole suite is the selection the scoped arming replaced."""
    run_id = "r-20261027T010100000000-node-a"
    manifest = tmp_path / "whole.md"
    whole = "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q tests/"
    _write_manifest(
        manifest,
        arms=(
            _suite_observation(whole, "base-abc"),
            _suite_observation(whole, "after-abc"),
        ),
    )
    _write_pointer(
        run_id, repository, suite_command=SCOPED, manifest_path=str(manifest)
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "baseline_suite.command_matches_suite_command" in payload["missing_fields"]
    assert "after_suite.command_matches_suite_command" in payload["missing_fields"]


def test_a_scoped_pair_over_another_nodes_files_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """Files outside the fenced scope are not this node's tests."""
    run_id = "r-20261027T010200000000-node-a"
    manifest = tmp_path / "other.md"
    other = RESOLVED.replace("tests/test_two.py", "tests/test_other.py")
    _write_manifest(
        manifest,
        arms=(
            _suite_observation(other, "base-abc"),
            _suite_observation(other, "after-abc"),
        ),
    )
    _write_pointer(
        run_id, repository, suite_command=SCOPED, manifest_path=str(manifest)
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "after_suite.command_matches_suite_command" in payload["missing_fields"]


def test_a_project_with_no_declaration_records_no_suite_observation(
    repository: Path, tmp_path: Path
) -> None:
    """With the declaration removed the run is unarmed and no pair is recorded."""
    run_id = "r-20261027T010300000000-node-a"
    manifest = tmp_path / "unarmed.md"
    _write_manifest(manifest)
    _write_pointer(run_id, repository, suite_command=None, manifest_path=str(manifest))
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["record"]["suite_delta"] is None
