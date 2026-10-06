"""A promotion commits every review round of the subject, not only the selected one.

A subject is reviewed once per round, so a first review at one head and a
re-review at another leave two stored records. Only the round selected for the
promoted head is committed, and every earlier round survives only in the host
staging store — invisible to a reader of the committed tree. These cases hold a
promotion to committing every stored round of the subject in the same commit as
its ledger row, so a review of an earlier head stays reachable by that head
after promotion. They hold the reader to falling through to the staging store
when the committed tree carries records for the run but none at the named head,
hold the store to taking a record's project from the promotion when the record
omits it, and hold the listing to finding a project whose reviews are all
committed, whose staging directory is gone, and which is known only through its
mount.

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
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "every-round-fixture"
PLAN = "every-round-target"
PLAN_SLUG = "every-round-plan"
PLAN_VERSION = 2

SUBJECT = "r-20261006T110000000000-reviewed-subject"
FIRST_REVIEW = "r-20261006T120000000000-first-review"
REVIEW = "r-20261006T130000000000-second-review"
PLAN_REVIEW_RUN = "r-20261006T140000000000-plan-review"
HEAD_ONE = "2e05df7191c5413efcfb9f3cc40b5f18b1f7a0bc"
HEAD_TWO = "775001135fd133d170a379fcf690336ce649cd11"
BASE = "a" * 40
DISPATCH_TS = "2026-10-06T11:00:00Z"
COMPLETION_TS = "2026-10-06T11:30:00Z"


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


def _write_run_record(repository: Path, run_id: str) -> Path:
    """Write one run's committed per-run record beside the ledger.

    The record is committed, because a review run's promotion refuses while its
    worktree shows repository changes it does not cite: the reviewed run's own
    record is a previously landed file, not this review run's work.
    """
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "dispatched_at": DISPATCH_TS,
                "completed_at": COMPLETION_TS,
            }
        ),
        encoding="utf-8",
    )
    _git(repository, "add", str(path.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "chore: land a run record")
    return path


def _manifest_body() -> str:
    return (
        "node: review-of-a-subject\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
    )


def _review_pointer(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    declared: list[str],
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
                "id": f"review-of-{SUBJECT}",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "role": "review",
                "write_paths": list(declared),
            },
        },
    )


def _run_review_record(review_run_id: str, head: str, *, total: int = 18) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": review_run_id,
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": head,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, total),
        "absent": [],
        "total": total * len(review_module.REVIEW_DIMENSIONS),
    }


def _plan_review_record() -> dict:
    return {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": PLAN_VERSION,
        "review_run_id": PLAN_REVIEW_RUN,
        "reviewed_blob_sha": "b" * 40,
        "status": "ready",
        "findings": [],
        "responses": {},
    }


def _committed_run_review(repository: Path, review_run_id: str) -> Path:
    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    return committed / "run" / SUBJECT / f"{review_run_id}.json"


def _commits_touching(repository: Path, relative: str) -> list[str]:
    out = _git(repository, "log", "--format=%H", "--", relative)
    return [line for line in out.splitlines() if line]


def _commit_paths(repository: Path, sha: str) -> set[str]:
    out = _git(repository, "show", "--name-only", "--format=", sha)
    return {line for line in out.splitlines() if line}


def _commit_subject(repository: Path, sha: str) -> str:
    return _git(repository, "log", "-1", "--format=%s", sha)


# ── (1) A promotion commits every stored review round of the subject ──────────


def test_promotion_commits_every_review_round_of_the_subject(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(repository, SUBJECT)
    # Two rounds of one subject at two heads, by two review runs.
    review_module.store_review(_run_review_record(FIRST_REVIEW, HEAD_ONE, total=16))
    second_path = review_module.store_review(
        _run_review_record(REVIEW, HEAD_TWO, total=18)
    )
    _review_pointer(repository, tmp_path, run_id=REVIEW, declared=[str(second_path)])

    crew.complete(REVIEW, gate="passed", root=repository)

    first_committed = _committed_run_review(repository, FIRST_REVIEW)
    second_committed = _committed_run_review(repository, REVIEW)
    assert first_committed.is_file()
    assert second_committed.is_file()

    row_rel = str(
        (
            repository / "docs" / "state" / PROJECT / "runs" / f"{REVIEW}.json"
        ).relative_to(repository)
    )
    first_rel = str(first_committed.relative_to(repository))
    second_rel = str(second_committed.relative_to(repository))
    # Both rounds ride exactly one commit, and that commit carries the ledger row
    # too: neither round adds a commit of its own beside the run's landing.
    [sha] = _commits_touching(repository, second_rel)
    paths = _commit_paths(repository, sha)
    assert row_rel in paths
    assert first_rel in paths
    assert _commit_subject(repository, sha) == f"promote({REVIEW}): passed"

    row = next(
        item
        for item in ledger.runs(PROJECT, root=repository)
        if item.get("run_id") == REVIEW
    )
    assert row["review"]["id"] == REVIEW

    # The earlier round stays reachable by the head it read, from the committed
    # tree alone.
    path, record = review_module.stored_record(
        PROJECT, SUBJECT, reviewed_head_sha=HEAD_ONE
    )
    assert path == first_committed
    assert record is not None and record["review_run_id"] == FIRST_REVIEW


# ── (2) The committed tree falls through to staging at an uncommitted head ────


def test_stored_record_falls_through_to_staging_for_a_head_the_tree_lacks(
    repository: Path,
) -> None:
    _write_run_record(repository, SUBJECT)
    # The committed tree carries the second round only, as a run promoted before
    # every round was committed would.
    review_module.store_committed_review(
        _run_review_record(REVIEW, HEAD_TWO, total=18), root=repository
    )
    # The first round lives only in staging, at a head the committed tree lacks.
    first_path = review_module.store_review(
        _run_review_record(FIRST_REVIEW, HEAD_ONE, total=16)
    )

    path, record = review_module.stored_record(
        PROJECT, SUBJECT, reviewed_head_sha=HEAD_ONE
    )
    assert path == first_path
    assert record is not None and record["review_run_id"] == FIRST_REVIEW

    # The committed round still answers for its own head.
    path, record = review_module.stored_record(
        PROJECT, SUBJECT, reviewed_head_sha=HEAD_TWO
    )
    assert path == _committed_run_review(repository, REVIEW)
    assert record is not None and record["review_run_id"] == REVIEW


# ── (3) A record whose body omits the project takes it from the promotion ─────


def test_a_record_without_a_project_takes_it_from_the_promotion(
    repository: Path,
) -> None:
    _write_run_record(repository, SUBJECT)
    payload = _run_review_record(FIRST_REVIEW, HEAD_ONE, total=16)
    payload.pop("project")
    # The store refuses a record carrying no project of its own only when the
    # caller names none either; a promoting run knows the project it lands into.
    path = review_module.store_committed_review(
        payload, project=PROJECT, root=repository
    )
    assert path.is_file()
    assert _read(path)["project"] == PROJECT


def test_a_promotion_lands_a_record_whose_body_omits_the_project(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(repository, SUBJECT)
    record = _run_review_record(REVIEW, HEAD_TWO, total=18)
    record.pop("project")
    stored_path = review_module.review_path(
        PROJECT, SUBJECT, reviewed_head_sha=HEAD_TWO
    )
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_text(json.dumps(record), encoding="utf-8")
    _review_pointer(repository, tmp_path, run_id=REVIEW, declared=[str(stored_path)])

    crew.complete(REVIEW, gate="passed", root=repository)

    committed = _committed_run_review(repository, REVIEW)
    assert committed.is_file()
    assert _read(committed)["project"] == PROJECT


# ── (4) A fully committed project is listed though staging is gone ────────────


def test_a_fully_committed_project_is_listed_with_no_staging_directory(
    repository: Path,
) -> None:
    _write_run_record(repository, PLAN_REVIEW_RUN)
    committed = review_module.store_committed_review(
        _plan_review_record(), root=repository
    )
    assert committed.is_file()
    # No staging directory exists for this project: its only review is committed.
    assert not (review_module.review_store_root() / PROJECT).exists()

    listed = [
        record
        for record in plan_review.list_plan_reviews()
        if record.get("plan_slug") == PLAN_SLUG
    ]
    assert len(listed) == 1
    assert listed[0]["review_path"] == str(committed)


def test_declined_recurrence_sees_a_project_known_only_through_its_mount(
    repository: Path,
) -> None:
    # A committed plan review carrying a declined finding: its recurrence count
    # is exactly what ``declined_recurrence`` walks the listing for, and the
    # project is reachable only through its mount.
    _write_run_record(repository, PLAN_REVIEW_RUN)
    record = _plan_review_record()
    record["findings"] = [{"id": "f1", "type": "duplicated-mechanism"}]
    record["responses"] = {"f1": {"action": "declined", "reason": "by design"}}
    review_module.store_committed_review(record, root=repository)
    assert not (review_module.review_store_root() / PROJECT).exists()

    recurrence = plan_review.declined_recurrence()
    assert recurrence["duplicated-mechanism"]["plans"] == [f"{PROJECT}/{PLAN_SLUG}"]
    assert recurrence["duplicated-mechanism"]["plan_count"] == 1
