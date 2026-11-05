"""A plan review's promotion receipt says whether the review was stored.

``crew complete`` prints ``store: written`` for a plan review whose record never
reaches the plan-review store: that field is the promoted run's own ledger index
append, correct for what it measures and silent about the review. The receipt
therefore carries ``plan_review_store``, naming whether the plan-review store
wrote the review and why not when it did not. A run that is not a plan review
carries no such field, and a promoting plan review whose composed report names
no run names the composed directory and the run id it fails to name — unless the
store already holds that delivery by its directory, which it is read back from
before anything is said about it.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import plan_review
from reckon.crew.runs import _write_json, crew_home, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "plan-review-receipt-fixture"
PLAN = "plan-review-receipt-target-plan"
PLAN_SLUG = "plan-review-receipt-plan"
PLAN_NODE = f"plan-review-of-{PLAN_SLUG}"
PLAN_VERSION = 5
BLOB = "b" * 40
COMPOSED_RUN_ID = f"r-20261103T010000000000-plan-review-of-{PLAN_SLUG}"
PROMOTING_RUN_ID = f"r-20261103T010001000000-plan-review-of-{PLAN_SLUG}"
RUN_REVIEW_RUN_ID = "r-20261103T010002000000-review-of-a-reviewed-run-abcdef123456"
RUN_REVIEW_NODE = "review-of-a-reviewed-run-abcdef123456"
DISPATCH_TS = "2026-11-03T01:00:01Z"

GOOD_REPORT = "RUBRIC wiring: pass — the plan declares a dependency that resolves.\n"


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


def _pointer(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    write_paths: list[str],
) -> None:
    manifest = tmp_path / f"{run_id}.manifest.md"
    manifest.write_text(_manifest_body(), encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
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
                "id": node_id,
                "plan": PLAN,
                "section": "",
                "time_budget": "20m",
                "role": "review",
                "write_paths": write_paths,
            },
        },
    )


def _composed_directory(
    *,
    dispatch_run_id: str | None = PROMOTING_RUN_ID,
    delivered: bool = True,
    rubric: str = "content",
) -> Path:
    """The composed report directory, its dispatch naming the promoting run.

    ``dispatch_run_id=None`` writes the shape a queued plan review leaves when
    its launch never joined the composed directory: ``run_id: null`` and a
    report beside it, which promotion cannot attribute to any run.
    """
    directory = plan_review.review_report_directory(PROJECT, PLAN_SLUG, COMPOSED_RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dispatch.json").write_text(
        json.dumps({"run_id": dispatch_run_id, "status": "dispatched"}),
        encoding="utf-8",
    )
    report_path = directory / "report.md"
    if delivered:
        report_path.write_text(GOOD_REPORT, encoding="utf-8")
    (directory / "plan-review.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "plan_slug": PLAN_SLUG,
                "plan_version": PLAN_VERSION,
                "rubric": rubric,
                "reviewed_blob_sha": BLOB,
                "plan_fingerprint": "fp",
                "section_digests": {},
                "report_path": str(report_path),
            }
        ),
        encoding="utf-8",
    )
    return directory


def _promote(repository: Path, run_id: str = PROMOTING_RUN_ID) -> dict:
    return crew.complete(run_id, gate="passed", root=repository)


def test_a_stored_plan_review_reads_written(repository: Path, tmp_path: Path) -> None:
    """A plan review the store wrote reports it, beside the ledger's store field."""
    _composed_directory()
    _pointer(
        repository,
        tmp_path,
        run_id=PROMOTING_RUN_ID,
        node_id=PLAN_NODE,
        write_paths=[str(_report_root() / COMPOSED_RUN_ID)],
    )

    receipt = _promote(repository)

    assert receipt["plan_review_store"]["status"] == "written", receipt[
        "plan_review_store"
    ]
    assert receipt["store"]["status"] == "written", receipt["store"]
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == PROMOTING_RUN_ID


def test_a_refused_store_names_its_reason_and_leaves_store_alone(
    repository: Path, tmp_path: Path
) -> None:
    """A review the store refused reports the refusal, while ``store`` stands."""
    directory = _composed_directory(rubric="not-a-rubric")
    _pointer(
        repository,
        tmp_path,
        run_id=PROMOTING_RUN_ID,
        node_id=PLAN_NODE,
        write_paths=[str(directory)],
    )

    receipt = _promote(repository)

    outcome = receipt["plan_review_store"]
    assert outcome["status"] == "failed", outcome
    assert "unknown review rubric" in outcome["error"], outcome
    assert receipt["store"]["status"] == "written", receipt["store"]
    # The reason the receipt names is the one the store recorded on the delivery.
    sidecar = _read(directory / "plan-review.json")
    assert "unknown review rubric" in str(sidecar.get("store_error") or ""), sidecar


def test_a_composed_report_naming_no_run_names_the_directory_and_the_run(
    repository: Path, tmp_path: Path
) -> None:
    """The silent miss reports the directory and the run id it fails to name."""
    directory = _composed_directory(dispatch_run_id=None)
    _pointer(
        repository,
        tmp_path,
        run_id=PROMOTING_RUN_ID,
        node_id=PLAN_NODE,
        write_paths=[str(directory)],
    )

    receipt = _promote(repository)

    outcome = receipt["plan_review_store"]
    assert outcome["status"] == "failed", outcome
    assert str(directory) in outcome["error"], outcome
    assert PROMOTING_RUN_ID in outcome["error"], outcome
    assert receipt["store"]["status"] == "written", receipt["store"]


def test_a_report_the_store_holds_by_directory_reads_written(
    repository: Path, tmp_path: Path
) -> None:
    """A delivery stored by its directory reads written, though no run is named."""
    directory = _composed_directory(dispatch_run_id=None)
    _pointer(
        repository,
        tmp_path,
        run_id=PROMOTING_RUN_ID,
        node_id=PLAN_NODE,
        write_paths=[str(directory)],
    )
    # The plan build's own pass, which joins a delivery by the directory it was
    # written from rather than by the run its dispatch names.
    assert plan_review.store_delivered_reviews(PROJECT, PLAN_SLUG) == []

    receipt = _promote(repository)

    assert receipt["plan_review_store"]["status"] == "written", receipt[
        "plan_review_store"
    ]
    assert receipt["store"]["status"] == "written", receipt["store"]


def test_a_run_review_receipt_carries_no_plan_review_store_field(
    repository: Path, tmp_path: Path
) -> None:
    """A run review's receipt has no such field at all, not a null one."""
    _pointer(
        repository,
        tmp_path,
        run_id=RUN_REVIEW_RUN_ID,
        node_id=RUN_REVIEW_NODE,
        write_paths=[],
    )

    receipt = _promote(repository, RUN_REVIEW_RUN_ID)

    assert "plan_review_store" not in receipt, sorted(receipt)
