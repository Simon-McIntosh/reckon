"""A promoted review run is never held for a pending-landing repair.

A review run has nothing to land: its deliverable is the record it stored, and
promotion commits that record with its ledger row. When the live pointer moves
under a promotion, the promotion keeps the pointer it could not remove and
marks it ``pending_landing_repair`` so the landing pass will finish the merge.
For a review that mark is a hold nothing lifts: ``crew complete --wait``
refuses a review, and the obligations hook lists the pointer as promotable-stale
for good. So a review run retires its pointer once its ledger row is committed,
whether the pointer moved or not, and a review pointer already left in the
stuck state is retired by the next sweep with nothing re-promoted and nothing
dropped from its row.

A temporary repository and crew home stand in for the project, and the real
crew home is asserted untouched, because an isolated read does not prove an
isolated write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon import crew, ledger
from reckon.crew import pending_landing, promotion
from reckon.crew.runs import _write_json, pointer_path, read_pointer
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "promoted-review-fixture"
PLAN = "promoted-review-target"
RUN_ID = "r-20261113T140142834510-promoted-review"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"


def _seed_repository(root: Path) -> None:
    plan = root / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        "</head><body><main class=plan-doc></main></body></html>\n",
        encoding="utf-8",
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (root / "candidate.txt").write_text("seed\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def the_real_pointer_directory_is_not_a_fixture_target() -> None:
    assert not (REAL_LIVE / f"{RUN_ID}.json").exists()
    yield
    assert not (REAL_LIVE / f"{RUN_ID}.json").exists()


@pytest.fixture()
def review_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A finished review run with a manifest and a live pointer, ready to promote."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "repo"
    repository.mkdir()
    _seed_repository(repository)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "candidate.txt"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    delivered = tmp_path / "reviews" / f"{RUN_ID}.json"
    scratch = tmp_path / "reviews" / "scratch.txt"
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: promoted-review\n"
        "status: complete\n"
        f"changed_paths: {delivered}, {scratch}\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "created_at": "2026-11-13T14:01:42Z",
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "attempt": 1,
            "phase": "delivered",
            "manifest_path": str(manifest),
            "node": {
                "id": "promoted-review",
                "plan": PLAN,
                "section": "review",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )
    return {
        "repository": repository,
        "config_home": config_home,
        "manifest": manifest,
    }


def _row_path(repository: Path) -> Path:
    return ledger.run_path(PROJECT, RUN_ID, repository)


def _move_the_pointer_while_promotion_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record a resume launch on the pointer after the landing writes commit."""
    real_commit = promotion._commit_landing_writes

    def commit_then_move(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        current = read_pointer(RUN_ID)
        current["resume_launch_in_flight"] = True
        _write_json(pointer_path(RUN_ID), current)
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_move)


def _start_a_newer_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the run again after the landing writes commit, as a resume does."""
    real_commit = promotion._commit_landing_writes

    def commit_then_resume(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        current = read_pointer(RUN_ID)
        current["attempt"] = int(current.get("attempt") or 1) + 1
        current["phase"] = "running"
        _write_json(pointer_path(RUN_ID), current)
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_resume)


def test_a_review_promoted_under_a_moved_pointer_retires_its_pointer(
    review_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _move_the_pointer_while_promotion_runs(monkeypatch)

    crew.complete(RUN_ID, gate="passed", root=review_run["repository"])

    assert _row_path(review_run["repository"]).is_file(), "the ledger row is absent"
    assert not pointer_path(RUN_ID).exists(), "the review's pointer stayed held"


def test_a_newer_attempts_pointer_is_left_in_place(
    review_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer a newer attempt wrote belongs to work still in flight."""
    _start_a_newer_attempt(monkeypatch)

    crew.complete(RUN_ID, gate="passed", root=review_run["repository"])

    kept = read_pointer(RUN_ID)
    assert kept["attempt"] == 2, kept
    assert kept["phase"] == "running", kept
    assert not kept.get("pending_landing_repair"), kept


def test_a_stuck_review_pointer_is_retired_by_the_next_sweep(
    review_run: dict[str, Any],
) -> None:
    repository = review_run["repository"]
    crew.complete(RUN_ID, gate="passed", root=repository)
    row_before = _row_path(repository).read_bytes()
    current = read_pointer(RUN_ID) if pointer_path(RUN_ID).exists() else None
    assert current is None
    pending_landing.mark_pending_landing_repair(
        RUN_ID,
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "role": "review",
            "attempt": 1,
            "phase": "promotion-requested",
            "pid": 0,
            "pid_start_time": 0,
        },
    )
    pending_landing.write_pending(RUN_ID, PROJECT, pointer=read_pointer(RUN_ID))

    results = pending_landing.finish_pending_landings()

    assert not pointer_path(RUN_ID).exists(), "the stuck review pointer stayed held"
    assert not list(pending_landing.pending_dir().glob(f"{RUN_ID}.json")), (
        "the run's pending landing record survived the retirement"
    )
    assert _row_path(repository).read_bytes() == row_before, "the ledger row changed"
    assert any(
        row.get("run_id") == RUN_ID and row.get("status", "").startswith("retired")
        for row in results
    ), results
