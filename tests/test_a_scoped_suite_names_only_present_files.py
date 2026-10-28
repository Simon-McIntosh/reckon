"""A scoped suite command names only the test files each tree holds.

The armed suite declaration is scoped with ``{node_test_files}``, which stands
for the node's fenced write paths. A write path is not a file that exists: a
node that replaces a test deletes one file and creates another, so the same
write scope names a different file list in the base tree and in the head tree.
Resolving it once, over the write paths alone, produced a head command naming
a deleted file — pytest then stopped with exit 4 having run nothing, and an
empty failure list read as a clean observation.

The command is therefore resolved over the files present in the tree each arm
runs against, and the arm comparison is made against that per-tree resolution
of the same template rather than against one literal string. An arm that ran
no test is refused as unmeasured instead of being credited with a clean delta.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.promotion_records import _scoped_suite_command
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"
TEMPLATE = "PYTHONPATH=. .venv/bin/python -m pytest -q {node_test_files}"
KEPT = "tests/test_kept.py"
DELETED = "tests/test_deleted.py"
ADDED = "tests/test_added.py"
WRITE_PATHS = [KEPT, DELETED, ADDED]


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository whose head deletes one test file and adds another."""
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
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    (root / "tests").mkdir()
    (root / KEPT).write_text("def test_kept():\n    assert True\n")
    (root / DELETED).write_text("def test_deleted():\n    assert True\n")
    _git(root, "add", "docs", "tests")
    _git(root, "commit", "-q", "-m", "seed base")
    (root / DELETED).unlink()
    (root / ADDED).write_text("def test_added():\n    assert True\n")
    _git(root, "add", "--", "tests")
    _git(root, "commit", "-q", "-m", "replace the deleted test with the added one")
    return root


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )


def _revision(root: Path, spec: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", spec],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _record(root: Path, *, base_sha: str) -> dict[str, object]:
    return {
        "suite_command": TEMPLATE,
        "worktree": str(root),
        "base_sha": base_sha,
        "node": {"write_paths": list(WRITE_PATHS)},
    }


def _observation(
    command: str,
    revision: str,
    *,
    exit_status: int = 0,
    failures: list[str] | None = None,
) -> dict[str, object]:
    failure_ids = list(failures or [])
    return {
        "revision": revision,
        "command": command,
        "exit_status": exit_status,
        "log_path": f"/durable/{revision[:8]}-{exit_status}.log",
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


def _write_pointer(
    run_id: str, root: Path, *, suite_command: str, manifest_path: str, base_sha: str
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-10-28T09:00:00Z",
            "manifest_path": manifest_path,
            "base_sha": base_sha,
            "suite_command": suite_command,
            "worktree": str(root),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§1",
                "time_budget": "20m",
                "write_paths": list(WRITE_PATHS),
            },
        },
    )


def _write_suite_manifest(
    path: Path, *, baseline: dict[str, object], after: dict[str, object]
) -> None:
    lines = ["node: node-a", "status: complete", "commits: abc123", "tests: done"]
    lines.append("baseline_suite: " + json.dumps(baseline))
    lines.append("after_suite: " + json.dumps(after))
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


def _complete(run_id: str, root: Path):
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
            str(root),
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


def test_each_tree_resolves_the_template_over_the_files_it_holds(
    repository: Path,
) -> None:
    """The head arm drops the deleted test and the base arm drops the new one."""
    base_sha = _revision(repository, "HEAD~1")
    head_sha = _revision(repository, "HEAD")
    record = _record(repository, base_sha=base_sha)

    at_base = _scoped_suite_command(record, revision=base_sha)
    at_head = _scoped_suite_command(record, revision=head_sha)

    assert DELETED in at_base and ADDED not in at_base
    assert ADDED in at_head and DELETED not in at_head
    assert KEPT in at_base and KEPT in at_head
    # Resolved over the write paths alone — the reading that produced the
    # measured defect — the command names both, which no single tree holds.
    over_scope = _scoped_suite_command(record)
    assert DELETED in over_scope and ADDED in over_scope


def test_a_pair_resolved_per_tree_is_accepted_though_no_arm_matches_the_scope(
    repository: Path, tmp_path: Path
) -> None:
    """Both arms match the template over their own tree, not one literal string."""
    base_sha = _revision(repository, "HEAD~1")
    head_sha = _revision(repository, "HEAD")
    record = _record(repository, base_sha=base_sha)
    at_base = _scoped_suite_command(record, revision=base_sha)
    at_head = _scoped_suite_command(record, revision=head_sha)
    assert at_base != at_head
    assert at_base != _scoped_suite_command(record)

    run_id = "r-20261028T090000000000-node-a"
    manifest = tmp_path / "per-tree.md"
    _write_suite_manifest(
        manifest,
        baseline=_observation(at_base, base_sha),
        after=_observation(at_head, head_sha),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=TEMPLATE,
        manifest_path=str(manifest),
        base_sha=base_sha,
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "clean"
    assert suite_delta["added_failure_ids"] == []
    assert suite_delta["baseline_measured_nothing"] is False


def test_an_after_arm_left_naming_no_test_file_is_refused_as_unmeasured(
    repository: Path, tmp_path: Path
) -> None:
    """A head arm that names only the deleted test measured nothing at all."""
    base_sha = _revision(repository, "HEAD~1")
    head_sha = _revision(repository, "HEAD")
    run_id = "r-20261028T090100000000-node-a"
    manifest = tmp_path / "head-collected-nothing.md"
    _write_suite_manifest(
        manifest,
        baseline=_observation(
            _scoped_suite_command(
                _record(repository, base_sha=base_sha), revision=base_sha
            ),
            base_sha,
        ),
        # The old resolution at the head: names the test this change deleted.
        after=_observation(
            TEMPLATE.replace("{node_test_files}", f"{KEPT} {DELETED} {ADDED}"),
            head_sha,
            exit_status=4,
        ),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=TEMPLATE,
        manifest_path=str(manifest),
        base_sha=base_sha,
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "after_suite.command_names_no_file" in payload["missing_fields"]


def test_an_arm_that_exited_having_collected_no_test_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """Exit 4 over files its own tree holds is a usage error, not a clean run."""
    base_sha = _revision(repository, "HEAD~1")
    head_sha = _revision(repository, "HEAD")
    record = _record(repository, base_sha=base_sha)
    at_base = _scoped_suite_command(record, revision=base_sha)
    at_head = _scoped_suite_command(record, revision=head_sha)
    run_id = "r-20261028T090200000000-node-a"
    manifest = tmp_path / "collected-nothing.md"
    _write_suite_manifest(
        manifest,
        baseline=_observation(at_base, base_sha),
        after=_observation(at_head, head_sha, exit_status=4),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=TEMPLATE,
        manifest_path=str(manifest),
        base_sha=base_sha,
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "after_suite.collected_no_test" in payload["missing_fields"]


def test_a_scope_whose_tests_the_change_creates_records_an_empty_baseline(
    repository: Path, tmp_path: Path
) -> None:
    """Every test in the scope is new, so the base tree holds none of them."""
    base_sha = _revision(repository, "HEAD~1")
    head_sha = _revision(repository, "HEAD")
    added_only = "tests/test_added.py"
    run_id = "r-20261028T090300000000-node-a"
    manifest = tmp_path / "created-tests.md"
    at_head = "PYTHONPATH=. .venv/bin/python -m pytest -q " + added_only
    _write_suite_manifest(
        manifest,
        # The base arm names the file its own tree lacks, which is the shape a
        # run dispatched before the per-tree rule records.
        baseline=_observation(at_head, base_sha, exit_status=4),
        after=_observation(at_head, head_sha),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=TEMPLATE,
        manifest_path=str(manifest),
        base_sha=base_sha,
    )
    _stored_review(run_id)
    pointer = pointer_path(run_id)
    stored = json.loads(pointer.read_text())
    stored["node"]["write_paths"] = [added_only]
    _write_json(pointer, stored)

    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "clean"
    assert suite_delta["baseline_measured_nothing"] is True


def _write_suite_manifest(
    path: Path, *, baseline: dict[str, object], after: dict[str, object]
) -> None:
    lines = ["node: node-a", "status: complete", "commits: abc123", "tests: done"]
    lines.append("baseline_suite: " + json.dumps(baseline))
    lines.append("after_suite: " + json.dumps(after))
    path.write_text("\n".join(lines) + "\n")
