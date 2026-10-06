"""A review run's promotion commits the record it delivered.

A review run's whole deliverable is the record it stored for the subject it
read, and the run makes no repository commit of its own, so promotion is the
one moment that record can be landed. These cases promote a run review and a
plan review in a synthesised project and hold the promotion to three facts:
each record lands in the project's ``docs/state/<project>/reviews/`` tree in
the same commit that carries the run's ledger row — no second commit is added —
the ledger row's ``review`` field names the record's id, and a record the
committed store cannot key or time refuses the promotion with neither the
record nor the ledger row committed.

The committed store itself is held to its own rule here too: a record carrying
no review run id is refused rather than filed under the run it reviews, so two
review runs of one reviewed run can never name the same committed file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import plan_review
from reckon.crew import review as review_module
from reckon.crew.review import review_path, store_committed_review
from reckon.crew.runs import _write_json, crew_home, pointer_path, read_pointer
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "review-record-commit-fixture"
PLAN = "review-record-commit-target"
PLAN_SLUG = "review-record-commit-plan"
PLAN_VERSION = 4
RUN_ID = "r-20261006T120000000000-review-of-a-landed-node"
REVIEWED_RUN = "r-20261006T110000000000-landed-node"

DISPATCH_TS = "2026-10-06T12:00:00Z"


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


def _manifest_body(declared: list[str]) -> str:
    return (
        "node: review-of-a-landed-node\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths:\n"
        + "".join(f"  - {path}\n" for path in declared)
        + f"tests: {EXECUTABLE_GATE_COMMAND}\n"
    )


def _review_pointer(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    declared: list[str],
) -> Path:
    manifest = tmp_path / f"{run_id}.manifest.md"
    manifest.write_text(_manifest_body(declared), encoding="utf-8")
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
                "section": "s4",
                "time_budget": "25m",
                "role": "review",
                "write_paths": list(declared),
            },
        },
    )
    return manifest


def _scores() -> dict[str, int]:
    return dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18)


def _run_review_staging_path() -> Path:
    return review_path(PROJECT, REVIEWED_RUN)


def _write_run_review() -> Path:
    """A complete run-review record stored where the dispatch granted it."""
    record_path = _run_review_staging_path()
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "reviewed_run_id": REVIEWED_RUN,
                "review_run_id": RUN_ID,
                "status": "parsed",
                "scores": _scores(),
                "absent": [],
                "total": 18 * len(review_module.REVIEW_DIMENSIONS),
            }
        ),
        encoding="utf-8",
    )
    return record_path


def _write_plan_review() -> Path:
    """A plan review stored in the staging store, keyed by its review run."""
    record_path = plan_review.plan_review_path(PROJECT, PLAN_SLUG, PLAN_VERSION)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "plan_slug": PLAN_SLUG,
                "plan_version": PLAN_VERSION,
                "review_run_id": RUN_ID,
                "status": "ready",
                "findings": [],
                "responses": {},
            }
        ),
        encoding="utf-8",
    )
    return record_path


def _commits_touching(repository: Path, relative: str) -> list[str]:
    out = _git(repository, "log", "--format=%H", "--", relative)
    return [line for line in out.splitlines() if line]


def _commit_paths(repository: Path, sha: str) -> set[str]:
    out = _git(repository, "show", "--name-only", "--format=", sha)
    return {line for line in out.splitlines() if line}


def _commit_subject(repository: Path, sha: str) -> str:
    return _git(repository, "log", "-1", "--format=%s", sha)


# ── (1) A run review's record lands in the ledger row's commit ───────────────


def test_a_run_reviews_record_lands_in_the_ledger_rows_commit(
    repository: Path, tmp_path: Path
) -> None:
    record_path = _write_run_review()
    _review_pointer(
        repository,
        tmp_path,
        run_id=RUN_ID,
        node_id="review-of-a-landed-node",
        declared=[str(record_path)],
    )

    crew.complete(RUN_ID, gate="passed", root=repository)

    committed = review_module.review_path(
        PROJECT,
        REVIEWED_RUN,
        committed_root=review_module.committed_review_root(PROJECT, root=repository),
        review_run_id=RUN_ID,
    )
    assert committed.is_file()

    row_rel = str(
        (
            repository / "docs" / "state" / PROJECT / "runs" / f"{RUN_ID}.json"
        ).relative_to(repository)
    )
    record_rel = str(committed.relative_to(repository))
    # The record rides exactly one commit, and that commit carries the row too:
    # the record adds no commit of its own beside the run's landing.
    [sha] = _commits_touching(repository, record_rel)
    assert row_rel in _commit_paths(repository, sha)
    assert _commit_subject(repository, sha) == f"promote({RUN_ID}): passed"

    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == RUN_ID
    assert row["review"]["id"] == RUN_ID
    assert _read(committed)["review_run_id"] == RUN_ID


# ── (2) A plan review's record lands in the ledger row's commit ──────────────


def test_a_plan_reviews_record_lands_in_the_ledger_rows_commit(
    repository: Path, tmp_path: Path
) -> None:
    record_path = _write_plan_review()
    _review_pointer(
        repository,
        tmp_path,
        run_id=RUN_ID,
        node_id=f"plan-review-of-{PLAN_SLUG}",
        declared=[str(record_path)],
    )

    crew.complete(RUN_ID, gate="passed", root=repository)

    committed = plan_review.plan_review_path(
        PROJECT,
        PLAN_SLUG,
        PLAN_VERSION,
        committed_root=review_module.committed_review_root(PROJECT, root=repository),
        review_run_id=RUN_ID,
    )
    assert committed.is_file()

    row_rel = str(
        (
            repository / "docs" / "state" / PROJECT / "runs" / f"{RUN_ID}.json"
        ).relative_to(repository)
    )
    record_rel = str(committed.relative_to(repository))
    [sha] = _commits_touching(repository, record_rel)
    assert row_rel in _commit_paths(repository, sha)
    assert _commit_subject(repository, sha) == f"promote({RUN_ID}): passed"

    [row] = ledger.runs(PROJECT, root=repository)
    assert row["review"]["id"] == RUN_ID
    assert _read(committed)["review_run_id"] == RUN_ID


# ── (3) The committed store refuses a record carrying no review run id ───────


def test_the_committed_store_refuses_a_record_with_no_review_run_id(
    repository: Path,
) -> None:
    with pytest.raises(ValueError, match="review run id"):
        store_committed_review(
            {
                "project": PROJECT,
                "reviewed_run_id": REVIEWED_RUN,
                "status": "parsed",
            },
            root=repository,
        )
    # Nothing is filed under the reviewed run's name.
    committed_root = review_module.committed_review_root(PROJECT, root=repository)
    assert committed_root is not None
    assert not (committed_root / "run" / REVIEWED_RUN).exists()


# ── (4) A record that cannot be written commits neither itself nor the row ───


def test_a_promotion_that_cannot_write_its_record_commits_nothing(
    repository: Path, tmp_path: Path
) -> None:
    # The delivered record is a complete run review with no review run id, so
    # the delivered lookup finds it and the committed store refuses it.
    record_path = _run_review_staging_path()
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "reviewed_run_id": REVIEWED_RUN,
                "status": "parsed",
                "scores": _scores(),
                "absent": [],
                "total": 18 * len(review_module.REVIEW_DIMENSIONS),
            }
        ),
        encoding="utf-8",
    )
    _review_pointer(
        repository,
        tmp_path,
        run_id=RUN_ID,
        node_id="review-of-a-landed-node",
        declared=[str(record_path)],
    )

    with pytest.raises(crew.CrewError):
        crew.complete(RUN_ID, gate="passed", root=repository)

    run_rel = f"docs/state/{PROJECT}/runs/{RUN_ID}.json"
    # Neither the ledger row nor a review record reached a commit: HEAD is still
    # the seed commit, the row is untracked, and no reviews path is tracked.
    head = _git(repository, "log", "-1", "--format=%H")
    assert _commit_subject(repository, head) == "test: seed repository"
    assert _git(repository, "ls-files", "--", run_rel) == ""
    assert not any(
        path.startswith(f"docs/state/{PROJECT}/reviews/")
        for path in _commit_paths(repository, head)
    )
    assert read_pointer(RUN_ID) is not None
