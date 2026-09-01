"""A promoted gate verdict must carry the check that produced it.

Before this, ``ledger.build_record`` stored whatever three-word gate string
the caller asserted with nothing behind it, and ``capabilities.derive_capabilities``
pooled every shadow observation regardless of whether its recorded
configuration actually isolated the backend swap. These tests cover both: the
CLI promotion path refuses an unfalsifiable passing gate and stores the check
when one is given, and capability derivation consults the single control
predicate rather than trusting ``lineage.kind`` alone.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import capabilities, ledger
from reckon.cli import main as cli_main
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
    return root


def _write_pointer(
    run_id: str,
    repository: Path,
    *,
    suite_command: str | None = None,
    manifest_path: str = "/durable/manifest.md",
    base_sha: str | None = None,
    worktree: str = "",
    write_paths: tuple[str, ...] = (),
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
            "base_sha": (
                base_sha
                if base_sha is not None
                else "base-abc"
                if suite_command
                else ""
            ),
            "suite_command": suite_command,
            "worktree": worktree,
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "20m",
                "write_paths": list(write_paths),
            },
        },
    )


def _suite_observation(revision: str, failure_ids: list[str]) -> dict[str, object]:
    return {
        "revision": revision,
        "command": "pytest -q",
        "exit_status": 1 if failure_ids else 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


def _write_suite_manifest(
    path: Path,
    *,
    baseline: dict[str, object] | None,
    after: dict[str, object] | None,
) -> None:
    lines = ["node: node-a", "status: complete", "commits: abc123", "tests: done"]
    if baseline is not None:
        lines.append("baseline_suite: " + json.dumps(baseline))
    if after is not None:
        lines.append("after_suite: " + json.dumps(after))
    path.write_text("\n".join(lines) + "\n")


def _complete_arguments(
    run_id: str,
    repository: Path,
    *,
    report_only: bool = True,
) -> list[str]:
    arguments = [
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        "--checkout-path",
        str(repository),
        "--gate-command",
        "pytest -q",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        "/durable/gate.log",
    ]
    if report_only:
        arguments.extend(("--no-commit", "report-only fixture"))
    return arguments


# ── The promotion path refuses an unfalsifiable passing gate ────────────────


def test_promoting_a_passing_gate_with_no_check_is_refused(repository: Path) -> None:
    run_id = "r-20260826T090000000000-node-a"
    _write_pointer(run_id, repository)

    result = CliRunner().invoke(
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
        ],
    )

    assert result.exit_code != 0
    assert "command" in result.output
    assert "exit_status" in result.output
    assert "log path or digest" in result.output
    # Refusal must not have consumed the pointer.
    assert pointer_path(run_id).is_file()


def test_promoting_a_passing_gate_with_a_full_check_is_accepted_and_stored(
    repository: Path,
) -> None:
    run_id = "r-20260826T090100000000-node-a"
    _write_pointer(run_id, repository)

    result = CliRunner().invoke(
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
            "uv run pytest tests/test_gate_evidence.py",
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/r-node-a/gate.log",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    stored = payload["record"]["gate_check"]
    assert stored["command"] == "uv run pytest tests/test_gate_evidence.py"
    assert stored["exit_status"] == 0
    assert stored["log_path"] == "/durable/r-node-a/gate.log"
    assert stored["log_digest"] is None


def test_a_log_digest_satisfies_the_check_in_place_of_a_log_path(
    repository: Path,
) -> None:
    run_id = "r-20260826T090200000000-node-a"
    _write_pointer(run_id, repository)

    result = CliRunner().invoke(
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
            "pytest",
            "--gate-exit-status",
            "0",
            "--gate-log-digest",
            "sha256:abc123",
        ],
    )

    assert result.exit_code == 0, result.output
    stored = json.loads(result.output)["record"]["gate_check"]
    assert stored["log_path"] is None
    assert stored["log_digest"] == "sha256:abc123"


def test_a_failing_or_not_run_gate_needs_no_check_evidence(repository: Path) -> None:
    run_id = "r-20260826T090300000000-node-a"
    _write_pointer(run_id, repository)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "failed",
            "--failure-classification",
            "work-rejected",
            "--outcome",
            "the diff did not compile",
            "--checkout-path",
            str(repository),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["record"]["gate_check"] is None


def test_armed_promotion_refuses_missing_observation_and_keeps_pointer(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260826T090400000000-node-a"
    manifest = tmp_path / "missing-after.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation("base-abc", ["tests/test_old.py::test_old"]),
        after=None,
    )
    _write_pointer(
        run_id,
        repository,
        suite_command="pytest -q",
        manifest_path=str(manifest),
    )

    result = CliRunner().invoke(cli_main, _complete_arguments(run_id, repository))

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "after_suite" in payload["missing_fields"]
    assert pointer_path(run_id).is_file()


def test_armed_promotion_refuses_added_failures_with_ids(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260826T090500000000-node-a"
    manifest = tmp_path / "added.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation("base-abc", ["tests/test_old.py::test_old"]),
        after=_suite_observation(
            "after-abc",
            ["tests/test_old.py::test_old", "tests/test_new.py::test_regression"],
        ),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command="pytest -q",
        manifest_path=str(manifest),
    )

    result = CliRunner().invoke(cli_main, _complete_arguments(run_id, repository))

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["added_failure_ids"] == ["tests/test_new.py::test_regression"]
    pointer = json.loads(pointer_path(run_id).read_text())
    assert pointer["suite_delta_refusal"]["status"] == "refused"


def test_reasoned_waiver_promotes_and_stores_observations(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260826T090600000000-node-a"
    manifest = tmp_path / "waived.md"
    baseline = _suite_observation("base-abc", ["tests/test_old.py::test_old"])
    after = _suite_observation(
        "after-abc",
        ["tests/test_old.py::test_old", "tests/test_new.py::test_regression"],
    )
    _write_suite_manifest(manifest, baseline=baseline, after=after)
    _write_pointer(
        run_id,
        repository,
        suite_command="pytest -q",
        manifest_path=str(manifest),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            *_complete_arguments(run_id, repository),
            "--waive-suite-delta",
            "known flaky host probe",
        ],
    )

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "waived"
    assert suite_delta["waiver_reason"] == "known flaky host probe"
    assert suite_delta["baseline_suite"]["revision"] == "base-abc"
    assert suite_delta["after_suite"]["revision"] == "after-abc"
    assert suite_delta["added_failure_ids"] == ["tests/test_new.py::test_regression"]


def test_repair_run_with_only_baseline_failures_is_clean(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260826T090700000000-node-a"
    manifest = tmp_path / "clean.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation(
            "base-abc", ["tests/test_old.py::test_one", "tests/test_old.py::test_two"]
        ),
        after=_suite_observation("after-abc", ["tests/test_old.py::test_two"]),
    )
    _write_pointer(
        run_id,
        repository,
        suite_command="pytest -q",
        manifest_path=str(manifest),
    )

    result = CliRunner().invoke(cli_main, _complete_arguments(run_id, repository))

    assert result.exit_code == 0, result.output
    suite_delta = json.loads(result.output)["record"]["suite_delta"]
    assert suite_delta["status"] == "clean"
    assert suite_delta["added_failure_ids"] == []


def test_real_cited_commit_and_clean_suite_delta_promote_together(
    repository: Path, tmp_path: Path
) -> None:
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "chore: seed repository"),
    ):
        subprocess.run(
            ["git", *arguments], cwd=repository, check=True, capture_output=True
        )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "change.txt").write_text("landed\n")
    subprocess.run(
        ["git", "add", "change.txt"], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "fix: land scoped change"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    landed_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = tmp_path / "real-run.md"
    _write_suite_manifest(
        manifest,
        baseline=_suite_observation(base_sha, ["tests/test_old.py::test_old"]),
        after=_suite_observation(landed_sha, ["tests/test_old.py::test_old"]),
    )
    run_id = "r-20260826T090800000000-node-a"
    _write_pointer(
        run_id,
        repository,
        suite_command="pytest -q",
        manifest_path=str(manifest),
        base_sha=base_sha,
        worktree=str(repository),
        write_paths=("change.txt",),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            *_complete_arguments(run_id, repository, report_only=False),
            "--commit",
            landed_sha,
        ],
    )

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)["record"]
    assert record["base_sha"] == base_sha
    assert record["commits"] == [landed_sha]
    assert record["suite_delta"]["status"] == "clean"
    assert record["suite_delta"]["added_failure_ids"] == []


# ── The single control predicate ─────────────────────────────────────────────


_BACKEND_ONLY_CONFIGURATION = {
    "substituted": {
        "backend": {"primary": "codex", "shadow": "candidate", "via": "backend"},
        "model": {"primary": "gpt-x", "shadow": "gpt-y", "via": "backend"},
    },
    "inherited": {"effort": "high", "sandbox": "worktree-full", "time_budget": "40m"},
}

_EFFORT_OVERRIDE_CONFIGURATION = {
    "substituted": {
        "backend": {"primary": "codex", "shadow": "candidate", "via": "backend"},
        "model": {"primary": "gpt-x", "shadow": "gpt-y", "via": "backend"},
        "effort": {"primary": "high", "shadow": "low", "via": "override"},
    },
    "inherited": {"sandbox": "worktree-full", "time_budget": "40m"},
}


def test_shadow_controlled_is_true_only_when_every_substitution_is_the_backend_swap() -> (
    None
):
    controlled_lineage = {
        "kind": "shadow",
        "primary_run_id": "r-primary",
        "configuration": _BACKEND_ONLY_CONFIGURATION,
    }
    confounded_lineage = {
        "kind": "shadow",
        "primary_run_id": "r-primary",
        "configuration": _EFFORT_OVERRIDE_CONFIGURATION,
    }

    assert ledger.shadow_controlled(controlled_lineage) is True
    assert ledger.shadow_controlled(confounded_lineage) is False
    # A shadow that never recorded a configuration cannot be verified.
    assert (
        ledger.shadow_controlled({"kind": "shadow", "primary_run_id": "r-x"}) is False
    )
    # Live lineage (or none at all) is not a shadow question.
    assert ledger.shadow_controlled(None) is False
    assert ledger.shadow_controlled({"kind": "live"}) is False


def test_completion_stamps_the_accessors_verdict_on_the_stored_lineage() -> None:
    controlled = ledger.build_record(
        run_id="r-controlled",
        plan=PLAN,
        gate="passed",
        lineage={
            "kind": "shadow",
            "primary_run_id": "r-primary",
            "configuration": _BACKEND_ONLY_CONFIGURATION,
        },
        agent={"backend": "candidate", "model": "gpt-y"},
    )
    confounded = ledger.build_record(
        run_id="r-confounded",
        plan=PLAN,
        gate="passed",
        lineage={
            "kind": "shadow",
            "primary_run_id": "r-primary",
            "configuration": _EFFORT_OVERRIDE_CONFIGURATION,
        },
        agent={"backend": "candidate", "model": "gpt-y"},
    )

    # The stored lineage is left exactly as the caller (e.g. shadow dispatch)
    # supplied it; the accessor's verdict lands as its own record field.
    assert controlled["lineage"] == {
        "kind": "shadow",
        "primary_run_id": "r-primary",
        "configuration": _BACKEND_ONLY_CONFIGURATION,
    }
    assert controlled["shadow_controlled"] is True
    assert confounded["shadow_controlled"] is False
    # The stamped value is exactly what the accessor itself returns — no
    # second implementation of the predicate exists to drift from it.
    assert controlled["shadow_controlled"] == ledger.shadow_controlled(
        controlled["lineage"]
    )
    assert confounded["shadow_controlled"] == ledger.shadow_controlled(
        confounded["lineage"]
    )


# ── Capability derivation never pools a confounded shadow with a controlled one ──


def _mounted(repository: Path) -> dict[str, str]:
    return {PROJECT: str(repository / "docs")}


def _seed_ledger(repository: Path, records: list[dict]) -> None:
    for record in records:
        ledger.append_run(PROJECT, record, root=repository)


def test_capability_derivation_labels_each_shadow_by_the_sole_predicate(
    repository: Path,
) -> None:
    """Two synthetic shadows of the same primary, differing only in inherited effort."""
    primary = ledger.build_record(
        run_id="r-primary",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        agent={"backend": "worker", "model": "concrete"},
    )
    controlled_shadow = ledger.build_record(
        run_id="r-shadow-controlled",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        lineage={
            "kind": "shadow",
            "primary_run_id": "r-primary",
            "configuration": _BACKEND_ONLY_CONFIGURATION,
        },
        agent={"backend": "candidate", "model": "gpt-y"},
    )
    confounded_shadow = ledger.build_record(
        run_id="r-shadow-confounded",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        lineage={
            "kind": "shadow",
            "primary_run_id": "r-primary",
            "configuration": _EFFORT_OVERRIDE_CONFIGURATION,
        },
        agent={"backend": "candidate", "model": "gpt-y"},
    )
    _seed_ledger(repository, [primary, controlled_shadow, confounded_shadow])

    derived = capabilities.derive_capabilities(_mounted(repository))

    assert len(derived["shadow_slices"]) == 1
    slice_ = derived["shadow_slices"][0]
    assert slice_["runs"] == 2
    by_run = {obs["run_id"]: obs for obs in slice_["observations"]}
    assert by_run["r-shadow-controlled"]["controlled"] is True
    assert by_run["r-shadow-confounded"]["controlled"] is False
    # The confounded row is never pooled into the controlled count.
    assert slice_["controlled_runs"] == 1


def test_qualification_depth_counts_distinct_primaries_not_shadow_rows(
    repository: Path,
) -> None:
    primary_one = ledger.build_record(
        run_id="r-primary-one",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        agent={"backend": "worker", "model": "concrete"},
    )
    primary_two = ledger.build_record(
        run_id="r-primary-two",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        agent={"backend": "worker", "model": "concrete"},
    )
    shadow_a = ledger.build_record(
        run_id="r-shadow-a",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        lineage={
            "kind": "shadow",
            "primary_run_id": "r-primary-one",
            "configuration": _BACKEND_ONLY_CONFIGURATION,
        },
        agent={"backend": "candidate", "model": "gpt-y"},
    )
    # A second controlled shadow of the SAME primary — demonstrates the
    # harness can repeat, not that a second node was qualified.
    shadow_b = ledger.build_record(
        run_id="r-shadow-b",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        lineage={
            "kind": "shadow",
            "primary_run_id": "r-primary-one",
            "configuration": _BACKEND_ONLY_CONFIGURATION,
        },
        agent={"backend": "candidate", "model": "gpt-y"},
    )
    shadow_c = ledger.build_record(
        run_id="r-shadow-c",
        plan=PLAN,
        gate="passed",
        worker_seconds=600,
        completed_at_source="provided",
        lineage={
            "kind": "shadow",
            "primary_run_id": "r-primary-two",
            "configuration": _BACKEND_ONLY_CONFIGURATION,
        },
        agent={"backend": "candidate", "model": "gpt-y"},
    )
    _seed_ledger(
        repository,
        [primary_one, primary_two, shadow_a, shadow_b, shadow_c],
    )

    derived = capabilities.derive_capabilities(_mounted(repository))

    assert len(derived["shadow_slices"]) == 1
    slice_ = derived["shadow_slices"][0]
    assert slice_["runs"] == 3
    assert slice_["controlled_runs"] == 3
    assert slice_["qualification_depth"] == 2
