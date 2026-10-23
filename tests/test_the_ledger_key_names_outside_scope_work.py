"""The ledger row names work outside a worker's scope under its current key.

The committed row's key was renamed from ``follow_on_paths`` to
``outside_scope_paths``. A promotion writes only the current key, and a row
written before the rename still reads through it: the old key is mapped on the
way in, by every reader, and is never written back. The manifest field the
paths are read from is the worker's own and keeps its name here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger, run_record, run_store
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

RENAMED_KEY = "outside_scope_paths"
RETIRED_KEY = "follow_on_paths"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'\n<meta name="docs-project" content="{PROJECT}">'
        f"\n<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _seed_git_repository(root)
    return root


def _seed_git_repository(root: Path) -> None:
    """Make the fixture a git worktree with one commit.

    Promotion refuses a checkout that cannot host the landing commit, and the
    ledger reads an absent crew.json as a deletion to recover unless git can
    report the path was never tracked — a question it answers only from a
    checkout that carries at least one commit.
    """
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "seed repository"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
        )


def _write_manifest(repository: Path, text: str, run_id: str) -> Path:
    manifests = repository.parent / "manifests"
    manifests.mkdir(exist_ok=True)
    path = manifests / f"{run_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _promote_with_manifest(repository: Path, run_id: str, manifest_text: str) -> dict:
    """Write a pointer with a manifest and promote the run."""
    manifest_path = str(_write_manifest(repository, manifest_text, run_id))
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-09-04T12:00:00Z",
        "manifest_path": manifest_path,
        "node": {
            "id": f"node-{run_id}",
            "plan": PLAN,
            "section": "§1",
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), pointer)
    _stored_review(run_id)
    return crew.complete(
        run_id,
        gate="passed",
        no_commit="test: the report is the deliverable",
        root=repository,
    )


def _stored_review(run_id: str) -> None:
    """Attach a complete review so the promotion records the row it measures."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    review_module.store_review(record)


def _row(repository: Path, run_id: str) -> dict:
    runs = ledger.load(PROJECT, repository)[0]["runs"]
    return next(item for item in runs if item["run_id"] == run_id)


def test_a_promotion_writes_the_renamed_key_and_not_the_retired_one(
    repository: Path,
) -> None:
    run_id = "r-20260904T120000000001-renamed"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\n"
        "follow_ons: reckon/crew/promotion.py is outside the fence, "
        "reckon/ledger.py\n"
        "blockers: none\n",
    )
    row = _row(repository, run_id)
    assert row[RENAMED_KEY] == ["reckon/crew/promotion.py", "reckon/ledger.py"]
    assert RETIRED_KEY not in row


def test_the_renamed_key_reads_through_record_store_and_ledger(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row carrying only the retired key reads under the renamed one.

    Every reader maps it, and none writes the retired key back.
    """
    paths = ["reckon/ledger.py", "reckon/crew/promotion.py"]

    # ... through run_record, on the row as a mapping.
    mapped = run_record.normalize_outside_scope_paths({RETIRED_KEY: list(paths)})
    assert mapped[RENAMED_KEY] == paths
    assert RETIRED_KEY not in mapped

    # ... through ledger, from a document written before the rename.
    run_id = "r-20260904T120000000002-old"
    ledger.run_path(PROJECT, run_id, repository).parent.mkdir(
        parents=True, exist_ok=True
    )
    ledger.write_run_file(
        PROJECT,
        {"run_id": run_id, "project": PROJECT, RETIRED_KEY: list(paths)},
        repository,
        mode=None,
    )
    row = _row(repository, run_id)
    assert row[RENAMED_KEY] == paths
    assert RETIRED_KEY not in row

    # ... through run_store, from a stored payload written before the rename.
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state-root"))
    store = run_store.RunStore(path=tmp_path / "run_store.db")
    store.append(
        PROJECT,
        {"run_id": run_id, "project": PROJECT, RETIRED_KEY: list(paths)},
    )
    stored = store.get_run(run_id)
    assert stored is not None
    assert stored[RENAMED_KEY] == paths
    assert RETIRED_KEY not in stored
    assert store.records()[run_id][RENAMED_KEY] == paths


def test_a_new_row_reads_through_the_same_key(repository: Path) -> None:
    run_id = "r-20260904T120000000003-new"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\nfollow_ons: reckon/run_store.py\nblockers: none\n",
    )
    row = _row(repository, run_id)
    assert row[RENAMED_KEY] == ["reckon/run_store.py"]
    assert run_record.normalize_outside_scope_paths(row)[RENAMED_KEY] == [
        "reckon/run_store.py"
    ]


def test_a_row_measured_with_no_paths_keeps_an_empty_list(repository: Path) -> None:
    """An empty list is a measurement, so it must not read as never measured."""
    run_id = "r-20260904T120000000004-empty"
    _promote_with_manifest(repository, run_id, "status: complete\nfollow_ons: none\n")
    row = _row(repository, run_id)
    assert row[RENAMED_KEY] == []
    assert run_record.normalize_outside_scope_paths(row)[RENAMED_KEY] == []
