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


def _write_pointer(run_id: str, repository: Path) -> None:
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
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


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
