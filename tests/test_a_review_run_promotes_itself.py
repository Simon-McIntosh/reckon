"""A completed review run promotes itself once its stored record parses.

A run in the review role has nothing to hand a coordinator: its whole
deliverable is the record it writes for the run it reads, so once that record
is on disk, its own manifest reads complete, and its worker and supervisor
have exited, the run promotes itself with ``promoted_by: review-self`` on its
ledger row rather than waiting for a hand step. This module fixes that
behaviour and its refusals:

- a complete review run with an exited worker and a stored parsed record is
  promoted with ``promoted_by: review-self`` and no longer counts as
  unreconciled at dispatch;
- one whose worker pid is alive is not promoted;
- one with no stored record is not promoted, and its reason is recorded;
- an implement run is never promoted by this path.

The fixture runs a temporary crew home and repository and asserts afterwards
that the workstation's real crew pointer directory is untouched, because an
isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.crew import recovery
from reckon.crew import review as review_module
from reckon.crew.recovery_review_acceptance import promote_review_run_itself
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-self-fixture"
PLAN = "review-self-target"
REVIEWED_RUN_ID = "r-20261012T120000000001-reviewed-run"
REVIEW_RUN_ID = "r-20261012T130000000000-review-of-reviewed-run"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{REVIEW_RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Review Self Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _record_path() -> Path:
    """The review-store path the reviewer was dispatched to write."""
    return review_module.review_path(PROJECT, REVIEWED_RUN_ID)


def _store_review_record() -> Path:
    """Write a complete parsed review record and return its path."""
    record = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "total": 20 * len(review_module.REVIEW_DIMENSIONS),
        "findings": [],
        "timestamp": "2026-10-12T13:00:00Z",
    }
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _write_review_pointer(
    repository: Path,
    tmp_path: Path,
    *,
    worker_pid: int | None,
    record_path: Path | None,
    role: str = "review",
    node_id: str = REVIEW_NODE_ID,
) -> None:
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    node: dict = {
        "id": node_id,
        "plan": PLAN,
        "section": "s2",
        "time_budget": "25m",
        "role": role,
    }
    if record_path is not None:
        node["write_paths"] = [str(record_path)]
    pointer: dict = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "created_at": "2026-10-12T13:00:00Z",
        "manifest_path": str(manifest),
        "node": node,
    }
    if worker_pid is not None:
        pointer["pid"] = worker_pid
    _write_json(pointer_path(REVIEW_RUN_ID), pointer)


def _write_exit_record() -> None:
    directory = Path(run_dir(REVIEW_RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}),
        encoding="utf-8",
    )


def _sweep() -> dict:
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )


def test_a_complete_review_run_promotes_itself(
    repository: Path, tmp_path: Path
) -> None:
    """Exited worker, stored parsed record: promoted with review-self."""
    record_path = _store_review_record()
    _write_review_pointer(
        repository, tmp_path, worker_pid=None, record_path=record_path
    )
    _write_exit_record()

    report = _sweep()

    assert report["self_promoted"] == [REVIEW_RUN_ID], report
    assert not pointer_path(REVIEW_RUN_ID).exists()
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == REVIEW_RUN_ID
    assert row["promoted_by"] == "review-self"
    # No longer unreconciled at dispatch: the pointer is gone, so the
    # reconciliation counter that reads live pointers cannot see it.
    assert recovery.overdue_unreconciled_runs(project=PROJECT, grace="0s") == []


def test_a_review_run_with_a_live_worker_is_not_promoted(
    repository: Path, tmp_path: Path
) -> None:
    """A live worker owns the run; the pointer must not be deleted."""
    record_path = _store_review_record()
    _write_review_pointer(
        repository, tmp_path, worker_pid=os.getpid(), record_path=record_path
    )
    _write_exit_record()

    report = _sweep()

    assert report["self_promoted"] == []
    assert pointer_path(REVIEW_RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_review_run_with_no_stored_record_is_not_promoted(
    repository: Path, tmp_path: Path
) -> None:
    """Nothing to read: the run is left for the coordinator, with a reason."""
    _write_review_pointer(
        repository, tmp_path, worker_pid=None, record_path=_record_path()
    )
    _write_exit_record()

    report = _sweep()

    assert report["self_promoted"] == []
    refusal = next(
        entry
        for entry in report["review_self_refused"]
        if entry.get("run_id") == REVIEW_RUN_ID
    )
    assert refusal["promoted"] is False
    assert "no readable record" in refusal["reason"]
    assert pointer_path(REVIEW_RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_implement_run_is_never_promoted_by_this_path(
    repository: Path, tmp_path: Path
) -> None:
    """The branch acts on review-role pointers only."""
    record_path = _store_review_record()
    _write_review_pointer(
        repository,
        tmp_path,
        worker_pid=None,
        record_path=record_path,
        role="implement",
        node_id="implement-target",
    )
    _write_exit_record()

    pointer = json.loads(pointer_path(REVIEW_RUN_ID).read_text(encoding="utf-8"))
    assert promote_review_run_itself(pointer) is None

    report = _sweep()
    assert report["self_promoted"] == []
