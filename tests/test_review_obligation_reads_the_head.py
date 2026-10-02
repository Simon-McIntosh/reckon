"""A review obligation reads the review of the run's own head.

A run whose stored review describes an earlier revision than its worktree head
reads ``review-missing``, naming the revision the review read and the head the
run now carries, never ``review-ready``. A run whose review is stored at its
current head reads ``review-ready``. Both cases are driven on a synthesised
temporary repository and a temporary configuration home, so no reader reaches
a real checkout or the operator's own crew home.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "head-obligation-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _commit(root, "seed.txt", "seed\n")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_complete_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    head: str,
) -> dict:
    """Record one complete, unpromoted run whose tree carries ``head``."""
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
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _store_review(*, run_id: str, base: str, head: str) -> Path:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    return review_module.store_review(record)


def _review_items(result: dict) -> list[dict]:
    return [
        item
        for item in result["obligations"]
        if item["kind"] in {"review-missing", "review-ready", "promotable-stale"}
    ]


def test_a_review_of_an_earlier_head_reads_review_missing(
    repository: Path, tmp_path: Path
) -> None:
    """The obligation names the review read and the head the run moved to."""
    run_id = "run-head-moved"
    reviewed_head = _git(repository, "rev-parse", "HEAD")
    head = _commit(repository, "repair.txt", "repair\n")
    _write_complete_run(
        repository, tmp_path, run_id=run_id, node_id="head-moved", head=head
    )
    _store_review(run_id=run_id, base=reviewed_head, head=reviewed_head)

    result = obligations_module.obligations(PROJECT, SESSION)

    items = _review_items(result)
    assert [item["kind"] for item in items] == ["review-missing"], items
    assert items[0]["run_id"] == run_id
    assert items[0]["reviewed_head"] == reviewed_head
    assert items[0]["head"] == head


def test_a_review_of_the_current_head_reads_review_ready(
    repository: Path, tmp_path: Path
) -> None:
    """A review stored at the head the worktree carries is the evidence it is."""
    run_id = "run-head-current"
    base = _git(repository, "rev-parse", "HEAD")
    head = _commit(repository, "more.txt", "more\n")
    _write_complete_run(
        repository, tmp_path, run_id=run_id, node_id="head-current", head=head
    )
    _store_review(run_id=run_id, base=base, head=head)

    result = obligations_module.obligations(PROJECT, SESSION)

    items = _review_items(result)
    assert [item["kind"] for item in items] == ["review-ready"], items
