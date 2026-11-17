"""A run's gate can state the command and partition its arms ran with.

A project arms one suite command for every node it dispatches. On a project
armed with a command pinned to the CPU, a node whose programs only run on a GPU
partition produces arms that can never match the arming, however sound they are
within themselves, and its work cannot land without re-running a command that
dies on the armed partition. A run's gate may therefore declare the command and
the partition its arms ran with: the comparison uses the declaration, and a
landing that would measure missing arms measures with the declared command on
the declared partition.

A pair that still cannot be matched is waived with a written reason, and the row
keeps that reason and the arms actually held. The one thing that waiver never
covers is a failure the held arms add: an arm that cannot be re-run is still
evidence of the run's state, and a regression inside that evidence is exactly
what the delta exists to catch.

A refusal is also cheap: the landing decides it from the record, so a refused
landing returns without submitting a scheduler job whose answer it would never
read.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import landing_measure
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

#: The command the project armed, pinned to the CPU where these programs die.
CPU_COMMAND = "JAX_PLATFORMS=cpu pytest -q tests/"
#: The command the run's arms actually ran, which its gate declares.
GPU_COMMAND = "pytest -q tests/"
GPU_PARTITION = "betelgeuse"


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
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed repository"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    # A run's base is the revision it started from and its head the one it
    # delivers, so the fixture needs both to be real commits.
    (root / "work.txt").write_text("the run's change\n")
    subprocess.run(
        ["git", "add", "work.txt"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "the run's change"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _revisions(repository: Path) -> tuple[str, str]:
    """The fixture's base and head revisions."""
    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return base, head


def _write_pointer(
    run_id: str,
    repository: Path,
    *,
    suite_command: str,
    manifest_path: str,
    base_sha: str,
    commits: list[str],
    declaration: dict[str, str] | None = None,
) -> None:
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-11-15T09:00:00Z",
        "manifest_path": manifest_path,
        "base_sha": str(base_sha),
        "commits": [str(sha) for sha in commits],
        "suite_command": str(suite_command),
        "worktree": str(repository),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§2",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    if declaration is not None:
        pointer["gate_declaration"] = declaration
    _write_json(pointer_path(run_id), pointer)


def _suite_observation(
    command: str, revision: str, failure_ids: list[str] | None = None
) -> dict[str, object]:
    return {
        "revision": revision,
        "command": command,
        "exit_status": 1 if failure_ids else 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": len(failure_ids or ()),
        "failure_ids": list(failure_ids or ()),
    }


def _write_suite_manifest(
    path: Path,
    *,
    baseline: dict[str, object],
    after: dict[str, object],
    commits: list[str],
) -> None:
    lines = [
        "node: node-a",
        "status: complete",
        "commits: " + " ".join(commits),
        "tests: done",
    ]
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


def _complete(run_id: str, repository: Path, *extra: str, land: bool = False):
    """Complete a run, presenting the commit its worktree delivers.

    The fixture's worktree sits one commit past its base, so the run made one
    and the landing cites it. Declaring ``--no-commit`` over a worktree that
    moved off its base is refused — the row would say the node succeeded with
    nothing pointing at its work — and this file's subject is the gate's
    declared arms, not the commitless form.
    """
    _base, head_sha = _revisions(repository)
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--wait",
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
            "--commit",
            head_sha,
            *(["--land", "--wait"] if land else []),
            *extra,
        ],
    )


def _declare_gate(run_id: str):
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "declare-gate",
            "--run",
            run_id,
            "--command",
            GPU_COMMAND,
            "--partition",
            GPU_PARTITION,
            "--reason",
            "the node's programs only run on the GPU partition",
        ],
    )


def _gpu_arms(
    tmp_path: Path,
    base_sha: str,
    head_sha: str,
    *,
    head_failure_ids: list[str] | None = None,
) -> Path:
    """Both arms ran the declared command, so only the arming disagrees."""
    manifest = tmp_path / "gpu-arms.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation(GPU_COMMAND, base_sha),
        after=_suite_observation(GPU_COMMAND, head_sha, head_failure_ids),
        commits=[head_sha],
    )
    return manifest


def test_declared_gpu_arms_land_and_the_row_names_them(
    repository: Path, tmp_path: Path
) -> None:
    """The declaration is what the pair is expected to run, and it lands."""
    run_id = "r-20261115T090000000000-node-a"
    base_sha, head_sha = _revisions(repository)
    manifest = _gpu_arms(tmp_path, base_sha, head_sha)
    _write_pointer(
        run_id,
        repository,
        suite_command=CPU_COMMAND,
        manifest_path=str(manifest),
        base_sha=base_sha,
        commits=[head_sha],
    )
    _stored_review(run_id)
    declared = _declare_gate(run_id)

    assert declared.exit_code == 0, declared.output
    assert json.loads(declared.output)["gate_declaration"]["partition"] == GPU_PARTITION
    result = _complete(run_id, repository)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "clean"
    assert suite_delta["suite_command"] == GPU_COMMAND
    assert suite_delta["added_failure_ids"] == []
    assert suite_delta["waiver_covered_fields"] == []
    assert suite_delta["waived_arms"] is None


def test_the_same_arms_undeclared_are_refused_naming_the_mismatch(
    repository: Path, tmp_path: Path
) -> None:
    """Undeclared, the pair differs from the arming and the refusal names both."""
    run_id = "r-20261115T090100000000-node-a"
    base_sha, head_sha = _revisions(repository)
    manifest = _gpu_arms(tmp_path, base_sha, head_sha)
    _write_pointer(
        run_id,
        repository,
        suite_command=CPU_COMMAND,
        manifest_path=str(manifest),
        base_sha=base_sha,
        commits=[head_sha],
    )
    _stored_review(run_id)

    result = _complete(run_id, repository)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "baseline_suite.command_matches_suite_command" in payload["missing_fields"]
    assert "after_suite.command_matches_suite_command" in payload["missing_fields"]
    assert "declare-gate" in payload["detail"]


def test_a_mismatch_waiver_lands_with_its_reason_and_the_held_arms(
    repository: Path, tmp_path: Path
) -> None:
    """The waiver is read before the refusal and the row keeps what it held."""
    run_id = "r-20261115T090200000000-node-a"
    base_sha, head_sha = _revisions(repository)
    manifest = _gpu_arms(tmp_path, base_sha, head_sha)
    _write_pointer(
        run_id,
        repository,
        suite_command=CPU_COMMAND,
        manifest_path=str(manifest),
        base_sha=base_sha,
        commits=[head_sha],
    )
    _stored_review(run_id)

    reason = "the armed command cannot run these programs on the GPU partition"
    result = _complete(run_id, repository, "--waive-suite-delta", reason)

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "waived"
    assert suite_delta["waiver_reason"] == reason
    assert suite_delta["waived_arms"]["reason"] == reason
    assert suite_delta["waived_arms"]["baseline_suite"]["command"] == GPU_COMMAND
    assert suite_delta["waived_arms"]["after_suite"]["command"] == GPU_COMMAND
    assert suite_delta["waived_arms"]["waived_fields"] == [
        "baseline_suite.command_matches_suite_command",
        "after_suite.command_matches_suite_command",
    ]
    assert suite_delta["added_failure_ids"] == []


def test_a_waiver_never_covers_a_failure_the_held_arms_add(
    repository: Path, tmp_path: Path
) -> None:
    """The same waiver still refuses the regression the pair reports."""
    run_id = "r-20261115T090300000000-node-a"
    base_sha, head_sha = _revisions(repository)
    manifest = _gpu_arms(
        tmp_path, base_sha, head_sha, head_failure_ids=["tests/test_one.py::test_new"]
    )
    _write_pointer(
        run_id,
        repository,
        suite_command=CPU_COMMAND,
        manifest_path=str(manifest),
        base_sha=base_sha,
        commits=[head_sha],
    )
    _stored_review(run_id)

    result = _complete(
        run_id,
        repository,
        "--waive-suite-delta",
        "the armed command cannot run these programs on the GPU partition",
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert payload["added_failure_ids"] == ["tests/test_one.py::test_new"]
    assert "never waives an added failure" in payload["detail"]


def test_a_refused_landing_returns_without_submitting_a_job(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal under --wait exits nonzero at once and submits nothing."""
    run_id = "r-20261115T090400000000-node-a"
    base_sha, head_sha = _revisions(repository)
    manifest = _gpu_arms(tmp_path, base_sha, head_sha)
    _write_pointer(
        run_id,
        repository,
        suite_command=CPU_COMMAND,
        manifest_path=str(manifest),
        base_sha=base_sha,
        commits=[head_sha],
    )
    _stored_review(run_id)
    submitted: list[tuple] = []

    def _record_submission(*args, **kwargs):
        submitted.append((args, kwargs))
        raise AssertionError("a refused landing submitted a measurement job")

    monkeypatch.setattr(landing_measure, "partition_submission", _record_submission)
    started = time.monotonic()
    result = _complete(run_id, repository, land=True)

    elapsed = time.monotonic() - started
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"] == "suite-delta-refused"
    assert submitted == []
    assert elapsed < 60
