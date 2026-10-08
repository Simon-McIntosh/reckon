"""A plan review delivered outside its report directory cannot promote.

A plan review is composed a report directory whose ``dispatch.json`` names the
crew run that runs it, and the run is told to write its ``report.md`` there. A
run that writes the report into a directory named for its own id instead leaves
the composed directory empty, so the delivery sits beside no ``plan-review.json``
sidecar: neither promotion nor the store reads it, and the review is lost with
nothing reported. These cases hold promotion to refusing exactly that delivery
-- naming both directories so the coordinator can move the report into place --
while a delivered report, a run that delivered nothing anywhere, and a run whose
composed directory cannot be found all promote as before.

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

PROJECT = "misdelivered-plan-review-fixture"
PLAN = "misdelivered-plan-review-target"
PLAN_SLUG = "misdelivered-plan-review-plan"
PLAN_NODE = f"plan-review-of-{PLAN_SLUG}"
PLAN_VERSION = 5
BLOB = "b" * 40
# The composed report directory carries this id; the crew run that produced it
# carries the promoting id below, which its dispatch.json names.
COMPOSED_RUN_ID = f"r-20261008T005220444149-plan-review-of-{PLAN_SLUG}"
PROMOTING_RUN_ID = f"r-20261008T005221071177-plan-review-of-{PLAN_SLUG}"
DISPATCH_TS = "2026-10-08T00:52:21Z"


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


def _report_root() -> Path:
    return plan_review._plan_review_report_root(PROJECT, PLAN_SLUG)


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
                "id": PLAN_NODE,
                "plan": PLAN,
                "section": "",
                "time_budget": "20m",
                "role": "review",
                "write_paths": [str(_report_root() / COMPOSED_RUN_ID)],
            },
        },
    )


def _write_composed_directory(*, delivered: bool) -> Path:
    """The composed report directory, its dispatch naming the promoting run.

    With ``delivered`` it carries the report and sidecar the store reads; without
    it, the directory holds the dispatch and sidecar but no ``report.md``, which
    is what a run that wrote its report elsewhere leaves behind.
    """
    directory = plan_review.review_report_directory(PROJECT, PLAN_SLUG, COMPOSED_RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dispatch.json").write_text(
        json.dumps({"run_id": PROMOTING_RUN_ID, "status": "dispatched"}),
        encoding="utf-8",
    )
    report_path = directory / "report.md"
    if delivered:
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
    return directory


def _write_misdelivered_report() -> Path:
    """A report written into a sibling directory named for the run's own id."""
    directory = _report_root() / PROMOTING_RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.md"
    report_path.write_text(
        "RUBRIC wiring: pass — the plan declares a dependency that resolves.\n",
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


# ── (1) A report beside the run's own id, and none in its assigned directory ──


def test_a_misdelivered_plan_review_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    assert COMPOSED_RUN_ID != PROMOTING_RUN_ID
    composed = _write_composed_directory(delivered=False)
    misdelivered = _write_misdelivered_report()
    _plan_review_pointer(repository, tmp_path)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(PROMOTING_RUN_ID, gate="passed", root=repository)

    message = str(refusal.value)
    assert str(composed) in message
    assert str(misdelivered) in message
    # Nothing landed: the refusal precedes any ledger row.
    assert ledger.runs(PROJECT, root=repository) == []


# ── (2) The report in its assigned directory promotes and commits the record ──


def test_a_delivered_plan_review_promotes_and_commits(
    repository: Path, tmp_path: Path
) -> None:
    _write_composed_directory(delivered=True)
    _plan_review_pointer(repository, tmp_path)

    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repository)

    committed = _committed_plan_review(repository)
    assert committed.is_file()
    assert _read(committed)["review_run_id"] == COMPOSED_RUN_ID
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == PROMOTING_RUN_ID


# ── (3) A run that delivered nothing anywhere promotes and commits nothing ────


def test_a_plan_review_that_delivered_nowhere_promotes(
    repository: Path, tmp_path: Path
) -> None:
    _write_composed_directory(delivered=False)
    _plan_review_pointer(repository, tmp_path)

    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repository)

    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == PROMOTING_RUN_ID
    assert not _committed_plan_review(repository).exists()
