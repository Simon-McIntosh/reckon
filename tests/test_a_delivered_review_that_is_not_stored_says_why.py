"""A delivered review the store refuses records its reason; coverage reads it.

A delivery that does not store leaves no stored review, so the gate's
missing-review refusal is the only thing the coordinator sees and the reason is
never written down. The store step now records the reason on the delivery's own
sidecar, under ``store_error``, and names it in the refusal. Beside that, a plan
covered section by section — but not by its whole-document fingerprint — reads
as reviewed through the coverage predicate, so a caller that passes the plan's
content and the gate return the same review.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module
from reckon.crew import plan_review

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        role: {"backend": "worker", "execution_capable": True}
        for role in ("implement", "investigate", "review", "test")
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}

PLAN_HTML = """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
<meta name="plan-title" content="Fixture">
<meta name="plan-status" content="active">
<meta name="plan-impl" content="0.0">
<meta name="plan-modified" content="2026-09-25">
<meta name="plan-version" content="1">
</head><body><h2 id="delivery">Delivery</h2><p>Ship one measured change.</p></body></html>
"""


@pytest.fixture
def reviewed_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    plan_path = plans / "fixture.html"
    plan_path.write_text(PLAN_HTML, encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo, plan_path


def _node(config_home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="delivery",
        goal="ship one measured change",
        plan="fixture",
        section="delivery",
        role="implement",
        spec_level="exact",
        done_when="pytest reports one passing plan review store case",
        write_paths=["src/change.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / "delivery.md"),
    )


def _plan(config_home: Path, repo: Path):
    return crew.plan_dispatch(
        node=_node(config_home),
        config={**CONFIG, "plan_review_gate": "enforce"},
        project="sample",
        repo=repo,
        base="HEAD",
    )


def test_a_refused_store_records_its_reason_on_the_sidecar_and_in_the_refusal(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    directory = plan_review.review_report_directory("sample", "fixture", "r-refused")
    directory.mkdir(parents=True)
    report_path = directory / "report.md"
    report_path.write_text(
        "RUBRIC wiring: pass — the plan declares a dependency that resolves.\n",
        encoding="utf-8",
    )
    # The delivered sidecar names no plan_version, which the store cannot key on,
    # so the delivery is refused and its reason belongs on the sidecar itself.
    (directory / "plan-review.json").write_text(
        json.dumps(
            {
                "project": "sample",
                "plan_slug": "fixture",
                "reviewed_blob_sha": "a" * 40,
                "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
                "rubric": "plan_review",
                "report_path": str(report_path),
            }
        ),
        encoding="utf-8",
    )

    refusals = plan_review.store_delivered_reviews("sample", "fixture")

    assert len(refusals) == 1
    reason = refusals[0]["store_error"]
    assert "plan_version" in reason
    sidecar = json.loads((directory / "plan-review.json").read_text(encoding="utf-8"))
    assert sidecar.get("store_error") == reason

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(config_home, repo)

    assert reason in str(excinfo.value)


def test_a_plan_covered_section_by_section_reads_as_reviewed_with_its_plan(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = reviewed_project
    run_id = "r-section-covered"
    directory = plan_review.review_report_directory("sample", "fixture", run_id)
    directory.mkdir(parents=True)
    # The snapshot the review composed for is the plan's current content, so its
    # unit digests recompute to the present ones; only the stored whole-document
    # fingerprint is stale, which is what makes coverage section-by-section.
    (directory / "plan.html").write_text(PLAN_HTML, encoding="utf-8")
    plan_review.store_plan_review(
        {
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": 1,
            "rubric": "plan_review",
            "reviewed_blob_sha": "a" * 40,
            "plan_fingerprint": "stale-whole-document-digest",
            "findings": [],
            "responses": {},
            "status": "ready",
            "review_run_id": run_id,
        },
        base_dir=config_home / "crew" / "reviews",
    )

    # Without the plan the whole-document match finds nothing: the review covers
    # the plan section by section and the stored fingerprint never matched.
    assert (
        plan_review.read_plan_review(
            "sample",
            "fixture",
            plan_fingerprint=plan_review._fingerprint_forms(plan_path),
        )
        is None
    )

    found = plan_review.read_plan_review("sample", "fixture", plan=plan_path)
    assert found is not None
    assert found["review_run_id"] == run_id

    # The gate takes the same record: in enforce mode it admits the build only
    # when a covering review is found.
    resolution = _plan(config_home, repo)
    assert resolution.validation.ok is True


def test_a_delivery_refused_once_clears_its_reason_when_it_stores(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    _config_home, _repo, plan_path = reviewed_project
    directory = plan_review.review_report_directory("sample", "fixture", "r-slow-store")
    directory.mkdir(parents=True)
    report_path = directory / "report.md"
    report_path.write_text(
        "RUBRIC wiring: pass — the plan declares a dependency that resolves.\n",
        encoding="utf-8",
    )
    # The sidecar names no plan_version, which the store cannot key on, so the
    # first store refuses the delivery and the reason lands on the sidecar
    # itself. The report file exists, so the delivery is listed and reaches the
    # store rather than being skipped as incomplete.
    sidecar_path = directory / "plan-review.json"
    sidecar_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "plan_slug": "fixture",
                "reviewed_blob_sha": "a" * 40,
                "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
                "rubric": "plan_review",
                "report_path": str(report_path),
            }
        ),
        encoding="utf-8",
    )

    first = plan_review.store_delivered_reviews("sample", "fixture")

    assert len(first) == 1
    reason = first[0]["store_error"]
    assert "plan_version" in reason
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar.get("store_error") == reason

    # The key the store needs arrives, written back through the one sidecar
    # writer with the recorded reason carried along, so the reason is present at
    # the store this pass rather than cleared by the repair.
    sidecar["plan_version"] = 1
    plan_review.write_review_sidecar(directory, payload=sidecar)
    assert (
        json.loads(sidecar_path.read_text(encoding="utf-8")).get("store_error")
        == reason
    )

    second = plan_review.store_delivered_reviews("sample", "fixture")

    assert second == []
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert "store_error" not in sidecar
    delivered = plan_review.delivered_reports("sample", "fixture")
    assert [entry["stored"] for entry in delivered] == [True]
