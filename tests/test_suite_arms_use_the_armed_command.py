"""An armed run's suite pair must measure the command it was armed with.

An armed promotion calculates its added-failure delta from the baseline and
after suite arms its manifest carries. Those arms are only comparable against
the run's own recorded ``suite_command``: a pair run with a different command
measured a different suite, so its delta describes some other selection of
tests. Promotion refuses such a pair, naming whichever arm does not match;
two arms that differ only in whitespace exercise the same command and are
accepted as before.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


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
    suite_command: str,
    manifest_path: str,
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
                "write_paths": [],
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


FOCUSED_PAIR = "pytest -q tests/test_one.py tests/test_two.py"
WHOLE_SUITE = "pytest -q tests/"


def test_focused_pair_against_a_whole_suite_arming_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """Both arms run a focused pair, but the run was armed with the whole suite."""
    run_id = "r-20261001T120000000000-node-a"
    manifest = tmp_path / "focused-pair.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation(FOCUSED_PAIR, "base-abc"),
        after=_suite_observation(FOCUSED_PAIR, "after-abc"),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=WHOLE_SUITE,
        manifest_path=str(manifest),
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "baseline_suite.command_matches_suite_command" in payload["missing_fields"]
    assert "after_suite.command_matches_suite_command" in payload["missing_fields"]


def test_pair_matching_the_arming_modulo_spacing_is_accepted(
    repository: Path, tmp_path: Path
) -> None:
    """Arms differing only in whitespace exercise the armed command and promote."""
    run_id = "r-20261001T120100000000-node-a"
    manifest = tmp_path / "spacing.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation("pytest  -q   tests/", "base-abc"),
        after=_suite_observation("pytest -q tests/ ", "after-abc"),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=WHOLE_SUITE,
        manifest_path=str(manifest),
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "clean"
    assert suite_delta["added_failure_ids"] == []


def test_one_arm_matching_the_arming_and_one_not_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """Only the arm whose command differs from the arming is named."""
    run_id = "r-20261001T120200000000-node-a"
    manifest = tmp_path / "one-arm.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation(WHOLE_SUITE, "base-abc"),
        after=_suite_observation(FOCUSED_PAIR, "after-abc"),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=WHOLE_SUITE,
        manifest_path=str(manifest),
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "after_suite.command_matches_suite_command" in payload["missing_fields"]
    assert (
        "baseline_suite.command_matches_suite_command" not in payload["missing_fields"]
    )
