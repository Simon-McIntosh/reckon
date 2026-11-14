"""A steady-state publish recomputes only the runs whose inputs moved.

The station derives every live session's obligations on each tick. Rebuilding a
run's review tier runs git and reads its plan; the held-worktree scan reads the
merged ledger and git's registry; the classifier rewrites each run's memo. None
of those answers changes between two ticks while the run's head, manifest,
plan, the ledger and the live pointer directory all hold still, so a second
tick should read none of them again.

The fixture is a synthesised crew home of six live runs, each in its own
worktree and each owing a review of its head, so every one of those paths is
reached on the first tick. Counts are taken through patches over the second
tick, and every payload is compared with the one the same state produces with
the memos cleared.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, review_records, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

ledger_module = importlib.import_module("reckon.ledger")
obligations_module = importlib.import_module("reckon.crew.obligations")
recovery_module = importlib.import_module("reckon.crew.recovery")
promotion_checks_module = importlib.import_module("reckon.crew.promotion_checks")
store_module = importlib.import_module("reckon._store")

PROJECT = "recompute-fixture"
SESSION = "coordinator-fixture"
LIVE_RUNS = 6
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised project of live runs that each owe a review of their head."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the recompute fixture"),
    ):
        _git(root, *arguments)

    # A plan document the review tier reads its declared risk from, so the
    # tier's plan input is a real file whose stat identity keys the memo.
    (root / "docs" / "plans" / "fixture-plan.html").write_text(
        '<html><head><meta name="plan-version" content="1"></head><body>'
        '<section data-reckon="section" data-id="fixture-section" '
        'data-capability-risk="light"></section></body></html>',
        encoding="utf-8",
    )

    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = root / "docs" / "state" / PROJECT
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    (ledger_dir / "crew.json").write_text(
        json.dumps({"data": {"_version": 1, "members": [], "runs": [], "holds": []}}),
        encoding="utf-8",
    )

    worktrees: dict[str, Path] = {}
    for index in range(LIVE_RUNS):
        run_id = f"run-{index:02d}"
        reviewed_head = _git(root, "rev-parse", "HEAD")
        worktree = tmp_path / f"worktree-{index:02d}"
        _git(root, "worktree", "add", "-q", "--detach", str(worktree), reviewed_head)
        head = _commit(worktree, f"reckon/work_{index:02d}.py", f"VALUE = {index}\n")
        worktrees[run_id] = worktree
        manifest = config_home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
            encoding="utf-8",
        )
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": SESSION,
                "repo": str(root),
                "worktree": str(worktree),
                "base_sha": reviewed_head,
                "process_alive": False,
                "launch": "in-harness",
                "role": "implement",
                "manifest_path": str(manifest),
                "node": {
                    "id": run_id,
                    "plan": "fixture-plan",
                    "section": "fixture-section",
                    "write_paths": ["seed.txt"],
                },
            },
        )
        emitted = "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
        record = review_module.parse_review(emitted)
        record.update(
            {
                "project": PROJECT,
                "reviewed_run_id": run_id,
                "review_run_id": f"review-of-{run_id}",
                "reviewed_base_sha": reviewed_head,
                "reviewed_head_sha": reviewed_head,
            }
        )
        review_module.store_review(record)
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return {
        "home": config_home,
        "repo": root,
        "ledger": ledger_dir,
        "worktrees": worktrees,
    }


def _payload(state: Any) -> dict[str, Any]:
    return obligation_snapshot.payload_for(state, SESSION)


def test_each_review_is_read_once_until_its_entries_move(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged pass reads no records; a store change invalidates one run."""
    obligations_module._clear_cached_obligation_inputs()
    state = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    assert len([row for row in state.rows if row.get("session") == SESSION]) >= 5
    first = _payload(state)
    assert len(first["obligations"]) >= 5

    directory = review_module.review_store_root() / PROJECT
    refreshed: list[Path] = []
    selected: list[str] = []
    stats: list[Path] = []
    reads: list[Path] = []
    real_refresh = review_records._refresh_store_entries
    real_select = recovery_module.select_review_for_head
    real_stat = os.stat
    real_read_text = Path.read_text

    def refresh(path: Path) -> Any:
        refreshed.append(path)
        return real_refresh(path)

    def select(project: str, run_id: str, head: str, **kwargs: Any) -> Any:
        selected.append(run_id)
        return real_select(project, run_id, head, **kwargs)

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(path, (str, bytes, os.PathLike)):
            target = Path(os.fsdecode(path))
            if target.suffix == ".json" and target.parent == directory:
                stats.append(target)
        return real_stat(path, *args, **kwargs)

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.suffix == ".json" and path.parent == directory:
            reads.append(path)
        return real_read_text(path, *args, **kwargs)

    # A known record proves that the stat instrument can see its target.
    known = next(directory.glob("*.json"))
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read_text)
    known.stat()
    known.read_text(encoding="utf-8")
    assert stats == [known]
    assert reads == [known]
    stats.clear()
    reads.clear()
    monkeypatch.setattr(review_records, "_refresh_store_entries", refresh)
    monkeypatch.setattr(recovery_module, "select_review_for_head", select)

    assert _payload(state) == first
    assert refreshed == []
    assert stats == [], "the indexed cohort needs no per-record stat"
    assert reads == []
    assert selected == [], selected

    run_id = "run-02"
    worktree = fleet["worktrees"][run_id]
    head = _git(worktree, "rev-parse", "HEAD")
    base = _git(worktree, "rev-parse", "HEAD^")
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": "another-review",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(record)
    selected.clear()
    stats.clear()
    reads.clear()
    refreshed.clear()
    changed = _payload(state)
    assert selected == [run_id], selected
    assert changed != first
    assert refreshed == []
    assert stats == [], "the writer indexed the new review without reader stats"
    assert reads, "the new review was read"
    assert all(path.name.startswith(f"{run_id}.") for path in reads), reads

    obligations_module._clear_cached_obligation_inputs()
    assert _payload(state) == changed


def test_an_in_place_rewrite_reselects_the_reviewed_run(fleet: dict[str, Any]) -> None:
    """A record rewritten at one path changes selection without a new entry."""
    obligations_module._clear_cached_obligation_inputs()
    state = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    before = _payload(state)
    run_id = "run-02"
    directory = review_module.review_store_root() / PROJECT
    path = next(directory.glob(f"{run_id}.at-*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["reviewed_head_sha"] = _git(fleet["worktrees"][run_id], "rev-parse", "HEAD")
    record["rewrite_probe"] = "the record changed at its existing path"
    review_records.record_written(path, record)

    after = _payload(state)
    assert after != before, "the current head now has a stored review"
    obligations_module._clear_cached_obligation_inputs()
    assert _payload(state) == after


def test_a_removed_tree_drops_its_held_record_after_pointer_churn(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deleted directory leaves Git's registry unchanged but stops holding."""
    held_tree = tmp_path / SESSION / "retained-tree"
    held_tree.mkdir(parents=True)
    record = {
        "run_id": "r-retained",
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": held_tree.name},
        "worktree_retention": {"worktree": str(held_tree)},
    }
    monkeypatch.setattr(
        obligations_module, "_registered_worktrees", lambda _root: [held_tree]
    )
    monkeypatch.setattr(
        obligations_module, "_run_namings", lambda *_args: ({}, [record])
    )
    monkeypatch.setattr(
        obligations_module, "_worktree_registry_signature", lambda _root: (1,)
    )
    obligations_module._clear_cached_obligation_inputs()
    first = obligations_module._held_worktree_records(PROJECT)
    assert held_tree in first

    calls = 0
    real_read = obligations_module._held_worktree_records_read

    def read(project: str, repository: Path) -> Any:
        nonlocal calls
        calls += 1
        return real_read(project, repository)

    monkeypatch.setattr(obligations_module, "_held_worktree_records_read", read)
    runs._write_json(
        runs.pointer_path("unrelated-pointer"),
        {"run_id": "unrelated-pointer", "project": PROJECT, "session": SESSION},
    )
    assert held_tree in obligations_module._held_worktree_records(PROJECT)
    assert calls == 0, "unrelated live-pointer writes do not re-read the ledger"

    held_tree.rmdir()
    assert held_tree not in obligations_module._held_worktree_records(PROJECT)


def test_a_delivered_review_is_validated_once_until_its_record_moves(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivered-record refusal shares the same bounded memo policy."""
    run_id = "run-02"
    directory = review_module.run_review_record_directory(runs.run_dir(run_id))
    directory.mkdir(parents=True)
    path = directory / "review.json"
    review_module._write_record(path, {"bad": True})
    monkeypatch.setattr(
        obligations_module, "_in_flight_review", lambda *_args: "review-run"
    )
    monkeypatch.setattr(obligations_module, "_live_pointers", lambda _project: [])
    monkeypatch.setattr(
        runs, "read_pointer", lambda _run_id: {"node": {"write_paths": [str(path)]}}
    )
    calls = 0

    def refusals(record: Any) -> list[str]:
        nonlocal calls
        calls += 1
        return ["missing project"] if record.get("bad") else []

    monkeypatch.setattr(review_module, "store_review_refusals", refusals)
    pointer = {"run_id": run_id, "project": PROJECT}
    obligations_module._clear_cached_obligation_inputs()
    first = obligations_module._refused_delivered_review(pointer)
    assert first is not None and "missing project" in first["reason"]
    assert obligations_module._refused_delivered_review(pointer) == first
    assert calls == 1

    review_module._write_record(path, {"bad": False})
    assert obligations_module._refused_delivered_review(pointer) is None
    assert calls == 2
