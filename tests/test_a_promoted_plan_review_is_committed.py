"""A promoted plan review is committed under the id its delivered report names.

A plan review's report directory and its stored record are keyed by one review
run id, while the crew run that ran the review carries a second, recorded in the
directory's ``dispatch.json``. Promotion resolves a run's delivered record by the
promoting run's own id, so it finds nothing for a plan review: the record is
filed under the composed id, not the crew run's. These cases promote a
plan-review run whose record carries a different review run id from the run and
whose delivered report is unstored at promotion, and hold the promotion to
committing that record under its own id — storing the delivered report first,
exactly as a plan build would, so the record is reachable from the staging store
as well as from the commit. The record rides the ledger row's own commit and the
row is keyed by the promoting run.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import plan_review
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, crew_home, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "promoted-plan-review-fixture"
PLAN = "promoted-plan-review-target"
PLAN_SLUG = "promoted-plan-review-plan"
PLAN_VERSION = 5
BLOB = "b" * 40
# The report directory and the stored record carry this composed id; the crew
# run that produced them carries the promoting id below.
COMPOSED_RUN_ID = f"r-20261007T141516205570-plan-review-of-{PLAN_SLUG}"
PROMOTING_RUN_ID = f"r-20261007T141519430181-plan-review-of-{PLAN_SLUG}"
DISPATCH_TS = "2026-10-07T14:15:43Z"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n',
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _manifest_body() -> str:
    return (
        "node: plan-review-of-a-plan\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
    )


def _plan_review_pointer(repository: Path, tmp_path: Path) -> None:
    """A live plan-review run pointer whose node names the reviewed plan."""
    manifest = tmp_path / f"{PROMOTING_RUN_ID}.manifest.md"
    manifest.write_text(_manifest_body(), encoding="utf-8")
    directory = plan_review.review_report_directory(PROJECT, PLAN_SLUG, COMPOSED_RUN_ID)
    _write_json(
        pointer_path(PROMOTING_RUN_ID),
        {
            "run_id": PROMOTING_RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "created_at": DISPATCH_TS,
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"plan-review-of-{PLAN_SLUG}",
                "plan": PLAN,
                "section": "",
                "time_budget": "20m",
                "role": "review",
                "write_paths": [str(directory)],
            },
        },
    )


def _write_delivered_report() -> Path:
    """A delivered plan-review report whose dispatch names the promoting run.

    The report directory is named for the composed review run id; its
    ``dispatch.json`` names the crew run that actually ran the review, which is
    the join between the promoting run and the record it delivered.
    """
    directory = plan_review.review_report_directory(PROJECT, PLAN_SLUG, COMPOSED_RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.md"
    report_path.write_text(
        "RUBRIC wiring: pass — the plan declares a dependency that resolves.\n",
        encoding="utf-8",
    )
    (directory / "plan-review.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "plan_slug": PLAN_SLUG,
                "plan_version": PLAN_VERSION,
                "rubric": "content",
                "reviewed_blob_sha": BLOB,
                "plan_fingerprint": "fp",
                "section_digests": {},
                "report_path": str(report_path),
            }
        ),
        encoding="utf-8",
    )
    (directory / "dispatch.json").write_text(
        json.dumps({"run_id": PROMOTING_RUN_ID, "status": "dispatched"}),
        encoding="utf-8",
    )
    return report_path


def _committed_plan_review(repository: Path) -> Path:
    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    return plan_review.plan_review_path(
        PROJECT,
        PLAN_SLUG,
        PLAN_VERSION,
        committed_root=committed,
        review_run_id=COMPOSED_RUN_ID,
    )


def _commits_touching(repository: Path, relative: str) -> list[str]:
    out = _git(repository, "log", "--format=%H", "--", relative)
    return [line for line in out.splitlines() if line]


def _commit_paths(repository: Path, sha: str) -> set[str]:
    out = _git(repository, "show", "--name-only", "--format=", sha)
    return {line for line in out.splitlines() if line}


def _commit_subject(repository: Path, sha: str) -> str:
    return _git(repository, "log", "-1", "--format=%s", sha)


# ── (1) The record lands under its own id, the run's id differs ───────────────


def test_a_promoted_plan_review_commits_the_record_its_report_names(
    repository: Path, tmp_path: Path
) -> None:
    assert COMPOSED_RUN_ID != PROMOTING_RUN_ID
    _write_delivered_report()
    _plan_review_pointer(repository, tmp_path)

    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repository)

    committed = _committed_plan_review(repository)
    assert committed.is_file()
    assert _read(committed)["review_run_id"] == COMPOSED_RUN_ID

    # The record rides the ledger row's commit and adds none of its own; the row
    # is keyed by the promoting run, not by the record's composed id.
    row_rel = str(
        (
            repository
            / "docs"
            / "state"
            / PROJECT
            / "runs"
            / f"{PROMOTING_RUN_ID}.json"
        ).relative_to(repository)
    )
    record_rel = str(committed.relative_to(repository))
    [sha] = _commits_touching(repository, record_rel)
    assert row_rel in _commit_paths(repository, sha)
    assert _commit_subject(repository, sha) == f"promote({PROMOTING_RUN_ID}): passed"

    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == PROMOTING_RUN_ID
    assert row["review"]["id"] == PROMOTING_RUN_ID


# ── (2) The delivered report is stored when it was unstored at promotion ──────


def test_promotion_stores_an_unstored_delivered_report_first(
    repository: Path, tmp_path: Path
) -> None:
    _write_delivered_report()
    _plan_review_pointer(repository, tmp_path)

    # The report is unstored: no stored record carries the composed id yet, so a
    # reader listing the plan's deliveries sees it as stored: false.
    before = plan_review.delivered_reports(PROJECT, PLAN_SLUG)
    assert len(before) == 1 and before[0]["stored"] is False

    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repository)

    # Promotion stored the delivered report through the plan-review store, so the
    # same listing now reads it as stored rather than leaving it only in the
    # committed tree.
    after = plan_review.delivered_reports(PROJECT, PLAN_SLUG)
    assert len(after) == 1 and after[0]["stored"] is True
    stored = review_module.record_for_review_run(PROJECT, COMPOSED_RUN_ID)
    assert stored is not None and stored[1]["review_run_id"] == COMPOSED_RUN_ID
