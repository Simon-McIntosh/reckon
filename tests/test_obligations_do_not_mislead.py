"""Obligation hints do not contradict the reflex or name a stale run.

``review-missing`` was listed with a ready-to-run dispatch command for a run
whose review the reflex had already launched, and ``worktree-held`` named a
promoted run whose path had been reused by a live run under the same node id.

Both cases are driven here on a synthetic repository and a temporary,
fully environment-resolved crew home.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "mislead-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed mislead fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(json.dumps({PROJECT: str(root / "docs")}))
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_complete_source(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    head: str,
    extra: dict | None = None,
) -> dict:
    """Record one complete, unpromoted run whose tree is at ``head``."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {node_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    pointer.update(extra or {})
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _write_review(
    *,
    run_id: str,
    node_id: str,
    write_paths: list[str],
    extra: dict | None = None,
) -> dict:
    """Record one live review run granted these record paths."""
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "role": "review",
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "role": "review",
            "time_budget": "20m",
            "write_paths": write_paths,
        },
    }
    pointer.update(extra or {})
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _stub_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 0}
    )


def test_a_reflex_launched_review_leaves_no_review_missing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live reflex claim on the reviewed run is not re-hinted as missing.

    The run re-completed after the review was launched, so its current head is
    not the one the review was granted. The reflex's own claim is still live and
    a retyped dispatch would be refused as a scope conflict, so no duty is owed.
    """
    run_id = "run-reflex-source"
    review_run_id = "run-reflex-review"
    reviewed_head = _git(repository, "rev-parse", "HEAD")
    (repository / "seed.txt").write_text("moved\n", encoding="utf-8")
    _git(repository, "add", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: the run re-completed")
    head = _git(repository, "rev-parse", "HEAD")
    _write_complete_source(
        repository,
        tmp_path,
        run_id=run_id,
        node_id="reflex-source",
        head=head,
        extra={
            recovery.REVIEW_DISPATCH_FIELD: {
                "status": "dispatched",
                "reason": "the review dispatched automatically",
                "run_id": review_run_id,
                "backend": "local",
                "at": (OBSERVED_AT - timedelta(seconds=120)).isoformat(),
                "attempt": 1,
            }
        },
    )
    _write_review(
        run_id=review_run_id,
        node_id="review-of-reflex-source",
        write_paths=[
            str(review_module.review_path(PROJECT, run_id)),
            str(
                review_module.review_path(
                    PROJECT, run_id, reviewed_head_sha=reviewed_head
                )
            ),
        ],
    )
    row = recovery.classify_pointer(
        runs.read_pointer(run_id), now_seconds=OBSERVED_AT.timestamp()
    )
    assert row["classification"] == "scoring"
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [row])
    _stub_drain(monkeypatch)

    result = obligations_module.obligations(PROJECT, SESSION)

    assert [item["kind"] for item in result["obligations"]] == []


def _write_ledger(repository: Path, rows: list[dict]) -> None:
    path = repository / "docs" / "state" / PROJECT / "crew.json"
    path.write_text(
        json.dumps(
            {
                "updated": OBSERVED_AT.isoformat(),
                "project": PROJECT,
                "doc": "crew",
                "data": {"members": [], "runs": rows, "holds": [], "_version": 1},
            }
        ),
        encoding="utf-8",
    )


def test_a_live_run_reusing_a_retained_path_is_not_hintable(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained path a live run occupies names that run, not the promoted one.

    ``node-reused`` was promoted and its ledger record still names the tree, but
    a live run now occupies that same path under the same node id. The collector
    leaves it alone, so no worktree-held hint may name the promoted run for it,
    while the tree nothing occupies is still hinted.
    """
    managed = tmp_path / "managed-worktrees" / SESSION
    managed.mkdir(parents=True)
    occupied = managed / "node-reused"
    free = managed / "node-retained"
    for path in (occupied, free):
        _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    retained_at = (OBSERVED_AT - timedelta(seconds=600)).isoformat()
    _write_ledger(
        repository,
        [
            {
                "run_id": "run-promoted-old",
                "plan": "fixture-plan",
                "node": "node-reused",
                "completed_at": retained_at,
                "worktree_retention": {
                    "worktree": str(occupied),
                    "retained_at": retained_at,
                },
            },
            {
                "run_id": "run-promoted-free",
                "plan": "fixture-plan",
                "node": "node-retained",
                "completed_at": retained_at,
                "worktree_retention": {
                    "worktree": str(free),
                    "retained_at": retained_at,
                },
            },
        ],
    )
    runs._write_json(
        runs.pointer_path("run-live-now"),
        {
            "run_id": "run-live-now",
            "project": PROJECT,
            "session": SESSION,
            "worktree": str(occupied),
            "process_alive": False,
            "phase": "running",
            "node": {
                "id": "node-reused",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [])
    _stub_drain(monkeypatch)

    result = obligations_module.obligations(PROJECT, SESSION)

    held = [item for item in result["obligations"] if item["kind"] == "worktree-held"]
    assert [item["run_id"] for item in held] == ["run-promoted-free"]
