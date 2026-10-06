"""The review readers read the committed store first, staging only when unpromoted.

A review is written to the host staging store, where the gate and acceptance
read it, and promoted into the project's committed ``docs/state/<project>/reviews/``
tree, where it travels with the plan and the ledger. Once promoted, the staging
file may be gone — the committed tree is the archive. These cases hold the
readers to that order: a run review resolves from the committed tree after its
staging file is deleted, an unpromoted review still reads from staging, a review
present in both trees is listed once as the committed copy, and promotion finds
a delivered plan review by its review run id through the review store's index
rather than by walking the project directory.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import plan_review, promotion
from reckon.crew import review as review_module

PROJECT = "reads-committed-first"
REVIEWED_RUN = "r-reviewed-run"
REVIEW_RUN = "r-review-run"
PLAN_SLUG = "demo"
PLAN_VERSION = 3
BLOB = "a" * 40
DISPATCH_TS = "2026-10-06T09:00:00+00:00"
COMPLETION_TS = "2026-10-06T09:07:30+00:00"


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout mounted at its own docs directory."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (config / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _run_record(root: Path, run_id: str) -> None:
    """Write one run's committed per-run record beside the ledger."""
    path = root / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
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


def _run_review_record() -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }


def _plan_review_record(review_run_id: str = REVIEW_RUN) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": PLAN_VERSION,
        "review_run_id": review_run_id,
        "rubric": "plan_review",
        "reviewed_blob_sha": BLOB,
        "plan_fingerprint": "fp",
        "findings": [],
        "responses": {},
        "status": "ready",
    }


def _write_staging(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _committed_run_review_path(root: Path) -> Path:
    committed = review_module.committed_review_root(PROJECT, root=root)
    assert committed is not None
    return committed / "run" / REVIEWED_RUN / f"{REVIEW_RUN}.json"


# ── (1) A promoted run review reads from the committed tree ──────────────────


def test_stored_record_reads_the_committed_tree_after_staging_is_deleted(
    root: Path,
) -> None:
    _run_record(root, REVIEW_RUN)
    committed_path = review_module.store_committed_review(
        _run_review_record(), root=root
    )
    assert committed_path == _committed_run_review_path(root)

    staging = _write_staging(
        review_module.review_path(PROJECT, REVIEWED_RUN), _run_review_record()
    )
    assert staging.is_file() and committed_path.is_file()

    # A review present in both trees is read from the committed copy, which is
    # the record that travels with the ledger.
    path, record = review_module.stored_record(PROJECT, REVIEWED_RUN)
    assert path == committed_path
    assert record is not None and record["review_run_id"] == REVIEW_RUN

    # A promoted run's staging file is gone; the committed copy still answers.
    staging.unlink()
    path, record = review_module.stored_record(PROJECT, REVIEWED_RUN)
    assert path == committed_path
    assert record is not None and record["review_run_id"] == REVIEW_RUN

    # read_review, the annotated reader, resolves through the same store.
    read = review_module.read_review(PROJECT, REVIEWED_RUN)
    assert read is not None and read["review_run_id"] == REVIEW_RUN


# ── (2) An unpromoted review still reads from staging ────────────────────────


def test_stored_record_reads_staging_for_an_unpromoted_run(root: Path) -> None:
    staging = _write_staging(
        review_module.review_path(PROJECT, REVIEWED_RUN), _run_review_record()
    )
    # No committed record exists for this run, so the resolver falls back to the
    # staging store where its live review was written.
    path, record = review_module.stored_record(PROJECT, REVIEWED_RUN)
    assert path == staging
    assert record is not None and record["review_run_id"] == REVIEW_RUN


# ── (3) The plan-review readers see the committed tree ───────────────────────


def test_plan_review_reads_the_committed_tree(root: Path) -> None:
    _run_record(root, REVIEW_RUN)
    committed_path = review_module.store_committed_review(
        _plan_review_record(), root=root
    )
    assert committed_path.is_file()
    # No staging file was written: the committed record is the only copy.
    assert not plan_review.plan_review_path(PROJECT, PLAN_SLUG, PLAN_VERSION).is_file()

    stored = plan_review.read_plan_review(PROJECT, PLAN_SLUG, PLAN_VERSION)
    assert stored is not None and stored["review_run_id"] == REVIEW_RUN

    listed = [
        record
        for record in plan_review.list_plan_reviews(PROJECT)
        if record.get("plan_slug") == PLAN_SLUG
    ]
    assert len(listed) == 1
    assert listed[0]["review_path"] == str(committed_path)


# ── (4) A review in both trees is listed once, as the committed copy ─────────


def test_a_review_in_both_trees_is_listed_once_as_the_committed_copy(
    root: Path,
) -> None:
    _run_record(root, REVIEW_RUN)
    committed_path = review_module.store_committed_review(
        _plan_review_record(), root=root
    )
    staging = _write_staging(
        plan_review.plan_review_path(PROJECT, PLAN_SLUG, PLAN_VERSION),
        _plan_review_record(),
    )
    assert staging.is_file() and committed_path.is_file()

    listed = [
        record
        for record in plan_review.list_plan_reviews(PROJECT)
        if record.get("plan_slug") == PLAN_SLUG
    ]
    assert len(listed) == 1
    # The single entry is the committed copy, not the staging one.
    assert listed[0]["review_path"] == str(committed_path)


# ── (5) Promotion finds a delivered plan review through the store index ──────


def test_promotion_finds_a_delivered_plan_review_through_the_store_index(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_staging(
        plan_review.plan_review_path(PROJECT, PLAN_SLUG, PLAN_VERSION),
        _plan_review_record(),
    )
    calls: list[tuple[str, str]] = []
    real = review_module.record_for_review_run

    def spy(project: str, review_run_id: str, *, base_dir=None):
        calls.append((project, review_run_id))
        return real(project, review_run_id, base_dir=base_dir)

    monkeypatch.setattr(review_module, "record_for_review_run", spy)

    payload = promotion._staging_review_record_by_run(PROJECT, REVIEW_RUN)
    # The lookup went through the review store's own index for the review run
    # id rather than walking the project directory.
    assert calls == [(PROJECT, REVIEW_RUN)]
    assert payload is not None and payload["review_run_id"] == REVIEW_RUN


def test_the_review_run_lookup_serves_from_the_store_index(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_staging(
        plan_review.plan_review_path(PROJECT, PLAN_SLUG, PLAN_VERSION),
        _plan_review_record(),
    )
    builds: list[Path] = []
    real_build = review_module._build_store_indexes

    def counting(directory: Path):
        builds.append(directory)
        return real_build(directory)

    monkeypatch.setattr(review_module, "_build_store_indexes", counting)
    review_module.record_for_review_run(PROJECT, REVIEW_RUN)
    assert len(builds) == 1
    # A second lookup for the same directory is served from the cached index
    # rather than rebuilding it.
    review_module.record_for_review_run(PROJECT, REVIEW_RUN)
    assert len(builds) == 1
