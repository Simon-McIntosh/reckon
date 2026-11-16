"""Report promotions read the grammar and identity their composer supplied."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.crew import plan_review, recovery, runs
from reckon.crew import review as review_module
from reckon.crew.recovery_review_acceptance import (
    accept_clean_review,
    promote_review_run_itself,
)
from tests import test_review_acceptance_promotes as acceptance_cases
from tests.helpers.git import git
from tests.test_a_review_run_promotes_itself import PLAN, PROJECT
from tests.test_a_review_run_promotes_itself import repository as report_repository

repository = report_repository
implementation_repository = acceptance_cases.repository

KINDS = ("composed", "code", "prose-commits", "portfolio")


def _deliver(root: Path, kind: str, *, manifest_text: str | None = None) -> dict:
    run_id = f"r-20261012T130000000000-review-{kind}"
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        manifest_text
        or (
            f"node: review-{kind}\nstatus: complete\n"
            "commits: none — the report is outside the repository\n"
            "changed_paths: []\ntests: reviewed the report and its findings\n"
        )
    )
    if kind in {"composed", "prose-commits"}:
        subject = f"r-20261012T120000000001-subject-{kind}"
        node_id = f"review-of-{subject}"
        path = review_module.review_path(PROJECT, subject, reviewed_head_sha="a" * 40)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": "parsed",
                    "project": PROJECT,
                    "reviewed_run_id": subject,
                    "review_run_id": run_id,
                    "reviewed_base_sha": "b" * 40,
                    "reviewed_head_sha": "a" * 40,
                    "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
                    "total": 120,
                    "findings": [],
                }
            )
        )
    elif kind == "plan":
        node_id = f"plan-review-of-{PLAN}"
        report_dir = plan_review.review_report_directory(PROJECT, PLAN, run_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / "report.md"
        path.write_text(
            "".join(
                f"RUBRIC {item}: pass\n" for item in review_module.PLAN_REVIEW_ITEMS
            )
        )
        runs._write_json(report_dir / "dispatch.json", {"run_id": run_id})
        plan_review.write_review_sidecar(
            report_dir,
            project=PROJECT,
            plan_slug=PLAN,
            plan_version=1,
            reviewed_blob_sha="b" * 40,
            document="<html><body>reviewed plan</body></html>",
            rubric="content",
            report_path=path,
            name="plan-review.json",
        )
    else:
        key = f"fixture-{kind}"
        if kind == "code":
            node_id = f"review-code-{key}"
            report_dir = plan_review.code_review_report_directory(
                project=PROJECT, key=key
            )
            sidecar_name = plan_review.CODE_REVIEW_SIDECAR_NAME
            sidecar = {"subject": "code", "neighbourhood_key": key}
            report = "".join(
                f"RUBRIC {item}: pass\n" for item in review_module.HEALTH_REVIEW_ITEMS
            )
            report += "TREATMENT: none — no repair required\n"
        else:
            node_id = f"review-portfolio-{key}"
            report_dir = plan_review.portfolio_review_report_directory(
                project=PROJECT, key=key
            )
            sidecar_name = plan_review.PORTFOLIO_SIDECAR_NAME
            sidecar = {
                "subject": "portfolio",
                "scope_key": key,
                "scope": "portfolio",
                "plans": [PLAN],
            }
            report = f"DISPOSITION {PLAN}: keep — the work remains useful\n"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / "report.md"
        path.write_text(report)
        runs._write_json(
            report_dir / sidecar_name,
            {**sidecar, "project": PROJECT, "review_run_id": f"composed-{kind}"},
        )
    worktree = root.parent / f"worker-{kind}"
    git(root, "worktree", "add", "--detach", "-q", str(worktree), "HEAD")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(root),
        "worktree": str(worktree),
        "base_sha": git(root, "rev-parse", "HEAD"),
        "role": "review",
        "launch": "in-harness",
        "backend": "native",
        "created_at": "2026-10-12T13:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": node_id,
            "plan": PLAN,
            "section": "s2",
            "role": "review",
            "write_paths": [str(path)],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    runs._write_json(directory / "exit.json", {"run_id": run_id, "exit_code": 0})
    return pointer


@pytest.mark.parametrize("passes", [1, 10])
def test_four_report_runs_promote_without_a_command(
    repository: Path, passes: int
) -> None:
    pointers = [_deliver(repository, kind) for kind in KINDS]
    reports = [
        recovery.dispatch_awaiting_reviews(project=PROJECT) for _ in range(passes)
    ]
    remaining = [
        p["run_id"] for p in pointers if runs.pointer_path(p["run_id"]).exists()
    ]
    assert remaining == [], json.dumps(reports, indent=2)
    rows = ledger.runs(PROJECT, root=repository)
    assert {r["run_id"] for r in rows} == {p["run_id"] for p in pointers}
    assert all(row["promoted_by"] == "review-self" for row in rows)
    assert all(row["gate"] == "not-run" for row in rows)


def test_manual_landing_keeps_every_report_delivered(repository: Path) -> None:
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    state = _plan_html.read_state(path.read_text())
    state["decisions"] = {
        "landing": {"choice": "manual", "rationale": "inspect reports"}
    }
    path.write_text(_plan_html.write_state(path.read_text(), state))
    git(repository, "add", str(path.relative_to(repository)))
    git(
        repository,
        "commit",
        "-m",
        "test: reserve landing",
        "-m",
        "Keep report publication with its coordinator.",
    )
    pointers = [_deliver(repository, kind) for kind in KINDS]
    report = recovery.dispatch_awaiting_reviews(project=PROJECT)
    assert all(runs.pointer_path(p["run_id"]).exists() for p in pointers), report
    assert ledger.runs(PROJECT, root=repository) == []
    assert all(
        "reserves landing" in entry["reason"] for entry in report["review_self_refused"]
    )


@pytest.mark.parametrize("kind", ["code", "portfolio"])
def test_an_unreadable_report_is_refused_with_its_condition(
    repository: Path, kind: str
) -> None:
    pointer = _deliver(repository, kind)
    Path(pointer["node"]["write_paths"][0]).write_text(
        "No structured verdict was delivered.\n"
    )
    result = promote_review_run_itself(pointer)
    assert not result["promoted"], result
    assert "report" in result["reason"], result
    assert runs.pointer_path(pointer["run_id"]).exists()


def test_a_plan_review_also_promotes_in_one_pass(repository: Path) -> None:
    pointer = _deliver(repository, "plan")
    report = recovery.dispatch_awaiting_reviews(project=PROJECT)
    assert report["self_promoted"] == [pointer["run_id"]], report
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["promoted_by"] == "review-self"
    assert row["gate"] == "not-run"


@pytest.mark.parametrize("kind", ["code", "portfolio"])
@pytest.mark.parametrize("condition", ["stale", "wrong-subject", "undeclared"])
def test_a_report_from_another_delivery_is_refused(
    repository: Path, kind: str, condition: str
) -> None:
    pointer = _deliver(repository, kind)
    path = Path(pointer["node"]["write_paths"][0])
    if condition == "stale":
        os.utime(path, (1, 1))
        expected = "not written after"
    elif condition == "wrong-subject":
        name = (
            plan_review.CODE_REVIEW_SIDECAR_NAME
            if kind == "code"
            else plan_review.PORTFOLIO_SIDECAR_NAME
        )
        sidecar = path.parent / name
        body = json.loads(sidecar.read_text())
        body["project"] = "another-project"
        sidecar.write_text(json.dumps(body))
        expected = "different subject"
    else:
        pointer["node"]["write_paths"] = []
        expected = "outside this run's declarations"
    result = promote_review_run_itself(pointer)
    assert not result["promoted"], result
    assert expected in result["reason"], result
    assert runs.pointer_path(pointer["run_id"]).exists()


def test_a_red_implementation_gate_keeps_its_failing_test_ids(
    implementation_repository: Path, tmp_path: Path
) -> None:
    root = implementation_repository
    acceptance_cases._land_and_review(root, tmp_path)
    worktree = tmp_path / "implementation-worker"
    git(root, "worktree", "add", "--detach", "-q", str(worktree), "HEAD")
    pointer = runs.read_pointer(acceptance_cases.RUN_ID)
    pointer["worktree"] = str(worktree)
    runs._write_json(runs.pointer_path(acceptance_cases.RUN_ID), pointer)
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_delivery_gate.py").write_text(
        "def test_contract():\n    assert False\n"
    )
    (root / "gate.sh").write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m pytest -q tests/test_delivery_gate.py\n"
    )
    git(root, "add", "gate.sh", "tests/test_delivery_gate.py")
    git(
        root,
        "commit",
        "-m",
        "test: expose an integrated gate failure",
        "-m",
        "The merged tree must retain its failing test identity.",
    )
    git(root, "push", "-q", "origin", "main")
    pointer = runs.read_pointer(acceptance_cases.RUN_ID)
    assert promote_review_run_itself(pointer) is None
    refusal = accept_clean_review(pointer)
    assert refusal is not None
    assert refusal["accepted"] is False, refusal
    assert refusal["gate_report"]["integrated_verdict"] == "failed"
    assert (
        "tests/test_delivery_gate.py::test_contract"
        in refusal["gate_report"]["finding"]["failure_ids"]
    )
    assert runs.pointer_path(acceptance_cases.RUN_ID).exists()
    assert ledger.runs(acceptance_cases.PROJECT, root=root) == []
