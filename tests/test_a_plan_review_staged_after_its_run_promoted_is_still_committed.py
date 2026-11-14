"""A plan review stored after its run promoted still reaches the committed store.

A promotion commits the review records it finds staged before it appends the
run's ledger row, so the row's commit carries them. A delivery that reaches the
store afterwards — a later pass over a plan whose review already landed — had no
promotion left to commit it, so its record stayed in the host staging store and
never travelled with the tree. The store now commits such a record itself,
through the same committed writer a promotion uses, whenever the crew run that
produced it already holds a ledger row. A record whose run has not promoted is
left staged, so its own promotion remains the one write that commits it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import plan_review
from reckon.crew import review as review_module

PROJECT = "sample"
SLUG = "fixture"
VERSION = 1
# The composed id the report directory is named for, and the crew run that
# actually ran the review — the two ids a plan review's delivery bridges.
COMPOSED = "r-20261018T110703328217-plan-review-of-fixture"
CREW_RUN = "r-20261018T111535867952-plan-review-of-fixture"
COMMIT = "b" * 40

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


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def project_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """A project checkout with a committed reviews tree and its mount entry."""
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
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo, plan_path


def _deliver(plan_path: Path) -> Path:
    """Deliver one plan review report and its sidecar, joined to its crew run."""
    directory = plan_review.review_report_directory(PROJECT, SLUG, COMPOSED)
    directory.mkdir(parents=True)
    report_path = directory / "report.md"
    report_path.write_text(
        "RUBRIC wiring: pass — the plan wires its own section.\n"
        "RUBRIC done_when: pass — the section states its measure.\n",
        encoding="utf-8",
    )
    plan_review.write_review_sidecar(
        directory,
        project=PROJECT,
        plan_slug=SLUG,
        plan_version=VERSION,
        reviewed_blob_sha="a" * 40,
        rubric=plan_review.CONTENT_RUBRIC,
        document=plan_path,
        report_path=report_path,
        plan_path=plan_path,
    )
    (directory / review_module.REVIEW_DISPATCH_FILE_NAME).write_text(
        json.dumps({"status": "dispatched", "run_id": CREW_RUN}),
        encoding="utf-8",
    )
    return directory


def _promote() -> None:
    """Record the crew run that delivered the review as a promoted run."""
    ledger.run_path(PROJECT, CREW_RUN).parent.mkdir(parents=True, exist_ok=True)
    ledger.write_run_file(
        PROJECT,
        {"run_id": CREW_RUN, "project": PROJECT, "promoted_revision": COMMIT},
        fsync=False,
    )


def _staging_path() -> Path:
    return plan_review.plan_review_path(PROJECT, SLUG, VERSION)


def _committed_path() -> Path:
    committed_root = review_module.committed_review_root(PROJECT)
    assert committed_root is not None
    return plan_review.plan_review_path(
        PROJECT,
        SLUG,
        VERSION,
        committed_root=committed_root,
        review_run_id=COMPOSED,
    )


def test_a_record_stored_after_its_run_promoted_reaches_the_committed_store(
    project_tree: tuple[Path, Path, Path],
) -> None:
    """The late pass commits the record it writes, through the promotion's writer."""
    _, repo, plan_path = project_tree
    _deliver(plan_path)
    _promote()

    refused = plan_review.store_delivered_reviews(PROJECT, SLUG)

    assert refused == []
    assert _staging_path().is_file()
    committed = _committed_path()
    assert committed.is_file(), f"the late record stayed staged at {_staging_path()}"
    body = json.loads(committed.read_text(encoding="utf-8"))
    assert body["review_run_id"] == COMPOSED
    assert body["plan_slug"] == SLUG
    # The write is committed to the checkout, not merely written into it, so the
    # record travels with the tree exactly as a promotion's own commit makes it.
    relative = committed.resolve().relative_to(repo.resolve())
    tracked = _git(repo, "ls-files", "--error-unmatch", str(relative))
    assert tracked.returncode == 0, tracked.stderr


def test_a_record_stored_before_its_run_promoted_stays_staged(
    project_tree: tuple[Path, Path, Path],
) -> None:
    """Writing before the run lands leaves the commit to that run's promotion."""
    _, _repo, plan_path = project_tree
    _deliver(plan_path)

    refused = plan_review.store_delivered_reviews(PROJECT, SLUG)

    assert refused == []
    assert _staging_path().is_file()
    assert not _committed_path().exists()
