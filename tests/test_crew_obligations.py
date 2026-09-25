"""Coordinator obligations stay a projection of recoverable fleet evidence."""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "obligation-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


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
        ("commit", "-q", "-m", "test: seed obligation fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_pointer(run_id: str, *, node: str | None = None) -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "node": {
                "id": node or run_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _file_mtimes(*roots: Path) -> dict[str, int]:
    """Snapshot every file mtime below the supplied evidence roots."""
    return {
        str(path): path.stat().st_mtime_ns
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
    }


def _row(
    run_id: str,
    classification: str,
    age: int,
    command: str,
    *,
    recovery_classification: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "session": SESSION,
        "plan": "fixture-plan",
        "node": f"node-{run_id}",
        "classification": classification,
        "recovery_classification": recovery_classification or classification,
        "terminal_age_seconds": age,
        "next_action": command,
    }


def _write_ledger(repository: Path, rows: list[dict[str, Any]]) -> None:
    path = repository / "docs" / "state" / PROJECT / "crew.json"
    path.write_text(
        json.dumps(
            {
                "updated": OBSERVED_AT.isoformat(),
                "project": PROJECT,
                "doc": "crew",
                "data": {
                    "members": [],
                    "runs": rows,
                    "holds": [],
                    "_version": 1,
                },
            }
        ),
        encoding="utf-8",
    )


def test_every_session_duty_has_identity_age_and_its_exact_next_command(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_rows = [
        _row(
            "run-review-missing",
            "scoring",
            100,
            "reckon crew dispatch --node review-run-review-missing",
        ),
        _row(
            "run-review-ready",
            "promotable",
            200,
            "reckon crew complete --run run-review-ready --gate <verdict>",
        ),
        _row(
            "run-needs-help",
            "blocked",
            300,
            "reckon crew resume --run run-needs-help --advice <answer>",
            recovery_classification="needs-help",
        ),
        _row(
            "run-ended-early",
            "interrupted",
            400,
            "reckon crew resume --run run-ended-early --advice continue",
        ),
        _row(
            "run-promotable-stale",
            "promotable",
            1_000,
            "reckon crew complete --run run-promotable-stale --gate <verdict>",
        ),
        _row(
            "run-working",
            "running",
            500,
            "reckon crew observe --run run-working",
        ),
    ]
    for row in live_rows:
        _write_pointer(str(row["run_id"]))

    held = tmp_path / "managed-worktrees" / SESSION / "node-held"
    held.parent.mkdir(parents=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(held), "HEAD")
    _write_ledger(
        repository,
        [
            {
                "run_id": "run-held",
                "plan": "fixture-plan",
                "node": "node-held",
                "completed_at": (OBSERVED_AT - timedelta(seconds=1_500)).isoformat(),
                "worktree_retention": {
                    "worktree": str(held),
                    "retained_at": (OBSERVED_AT - timedelta(seconds=1_500)).isoformat(),
                },
            },
            {
                "run_id": "run-promoted",
                "plan": "fixture-plan",
                "node": "node-promoted",
                "completed_at": (OBSERVED_AT - timedelta(seconds=2_000)).isoformat(),
            },
        ],
    )
    monkeypatch.setattr(
        obligations_module,
        "_classified_rows",
        lambda _project: live_rows,
    )
    monkeypatch.setattr(
        runs,
        "drain",
        lambda project, session=None: {
            "project": project,
            "session": session,
            "unreconciled_runs": 7,
        },
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    items = {item["kind"]: item for item in result["obligations"]}
    assert set(items) == {
        "review-missing",
        "review-ready",
        "needs-help",
        "turn-ended-early",
        "promotable-stale",
        "worktree-held",
    }
    assert len(result["obligations"]) == 6
    assert {item["run_id"] for item in result["obligations"]}.isdisjoint(
        {"run-working", "run-promoted"}
    )
    assert {kind: item["age_seconds"] for kind, item in items.items()} == {
        "review-missing": 100,
        "review-ready": 200,
        "needs-help": 300,
        "turn-ended-early": 400,
        "promotable-stale": 1_000,
        "worktree-held": 1_500,
    }
    assert items["review-missing"]["node"] == "node-run-review-missing"
    assert items["review-missing"]["next_command"] == (
        "reckon crew dispatch --node review-run-review-missing"
    )
    assert items["review-ready"]["next_command"] == (
        "reckon crew complete --run run-review-ready --gate <verdict>"
    )
    assert items["needs-help"]["next_command"] == (
        "reckon crew resume --run run-needs-help --advice <answer>"
    )
    assert items["turn-ended-early"]["next_command"] == (
        "reckon crew resume --run run-ended-early --advice continue"
    )
    assert items["promotable-stale"]["next_command"] == (
        "reckon crew complete --run run-promotable-stale --gate <verdict>"
    )
    assert items["worktree-held"]["next_command"] == (
        f"reckon crew gc --repo {repository} --project {PROJECT} --apply"
    )
    assert result["summary"] == {
        "count": 6,
        "oldest_age_seconds": 1_500,
        "unreconciled_runs": 7,
    }


def test_obligations_reads_scoring_without_observing_or_dispatching(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-pure-read"
    config_home = repository.parent / "config"
    head = _git(repository, "rev-parse", "HEAD")
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir()
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
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": "pure-read",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    def side_effect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a read reached a mutating dispatch or observe entry point")

    monkeypatch.setattr(dispatch_module, "dispatch", side_effect)
    monkeypatch.setattr(dispatch_module, "observe", side_effect)
    before = _file_mtimes(config_home, repository / "docs")

    result = obligations_module.obligations(PROJECT, SESSION)

    after = _file_mtimes(config_home, repository / "docs")
    assert [item["kind"] for item in result["obligations"]] == ["review-missing"]
    assert result["obligations"][0]["run_id"] == run_id
    assert after == before


@pytest.mark.parametrize(
    ("recovery_classification", "expected"),
    [("blocked", "blocked"), ("needs-help", "needs-help")],
)
def test_blocked_recovery_causes_keep_their_distinct_kind(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_classification: str,
    expected: str,
) -> None:
    row = _row(
        "run-blocked",
        "blocked",
        90,
        "reckon crew resume --run run-blocked",
        recovery_classification=recovery_classification,
    )
    _write_pointer("run-blocked")
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [row])
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 1}
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    assert [item["kind"] for item in result["obligations"]] == [expected]


def _store_complete_review(run_id: str, *, base: str, head: str) -> None:
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
    review_module.store_review(record)


def test_a_review_of_an_earlier_head_is_still_missing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-head-moved"
    base = _git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / "head-moved-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    (worktree / "seed.txt").write_text("new head\n", encoding="utf-8")
    _git(worktree, "add", "seed.txt")
    _git(worktree, "commit", "-q", "-m", "test: move reviewed head")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir()
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(worktree),
        "base_sha": base,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": "head-moved",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    _store_complete_review(run_id, base=base, head=base)
    row = recovery.classify_pointer(pointer, now_seconds=OBSERVED_AT.timestamp())
    assert row["classification"] == "scoring"
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [row])
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 1}
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    assert [item["kind"] for item in result["obligations"]] == ["review-missing"]


def test_a_legacy_review_record_of_the_current_head_is_review_ready(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-legacy-review"
    head = _git(repository, "rev-parse", "HEAD")
    manifest = tmp_path / "legacy-manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
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
            "id": "legacy-review",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    review = review_module.parse_review(emitted)
    review.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "timestamp": "2030-01-01T00:00:00+00:00",
        }
    )
    legacy = review_module.store_review(review)
    assert legacy == review_module.review_path(PROJECT, run_id)
    assert not review_module.review_path(
        PROJECT, run_id, reviewed_head_sha=head
    ).exists()
    row = recovery.classify_pointer(pointer, now_seconds=OBSERVED_AT.timestamp())
    assert row["classification"] == "promotable"
    row["terminal_age_seconds"] = 100
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [row])
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 1}
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    assert [item["kind"] for item in result["obligations"]] == ["review-ready"]


def test_a_review_in_flight_for_an_older_head_does_not_suppress_missing(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "run-source-old-review"
    review = "run-review-old-head"
    old_head = _git(repository, "rev-parse", "HEAD")
    (repository / "seed.txt").write_text("current head\n", encoding="utf-8")
    _git(repository, "add", "seed.txt")
    _git(repository, "commit", "-q", "-m", "test: advance review source")
    _write_pointer(source, node="source-old-review")
    source_pointer = runs.read_pointer(source)
    source_pointer.update({"repo": str(repository), "worktree": str(repository)})
    runs._write_json(runs.pointer_path(source), source_pointer)
    _write_pointer(review, node="review-of-source-old-review")
    review_pointer = runs.read_pointer(review)
    review_pointer["node"]["write_paths"] = [
        str(review_module.review_path(PROJECT, source)),
        str(review_module.review_path(PROJECT, source, reviewed_head_sha=old_head)),
    ]
    runs._write_json(runs.pointer_path(review), review_pointer)
    row = _row(
        source,
        "scoring",
        60,
        "reckon crew dispatch --node review-of-source-old-review",
    )
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [row])
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 2}
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    assert [item["kind"] for item in result["obligations"]] == ["review-missing"]


def test_a_review_from_another_session_for_the_current_head_suppresses_missing(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "run-source"
    review = "run-review"
    head = _git(repository, "rev-parse", "HEAD")
    keyed = review_module.review_path(PROJECT, source, reviewed_head_sha=head)
    _write_pointer(source, node="source-node")
    source_pointer = runs.read_pointer(source)
    source_pointer.update({"repo": str(repository), "worktree": str(repository)})
    runs._write_json(runs.pointer_path(source), source_pointer)
    _write_pointer(review, node="review-of-source-node")
    review_pointer = runs.read_pointer(review)
    review_pointer["session"] = "peer-coordinator"
    review_pointer["node"]["write_paths"] = [
        str(review_module.review_path(PROJECT, source)),
        str(keyed),
    ]
    runs._write_json(runs.pointer_path(review), review_pointer)
    row = _row(
        source,
        "scoring",
        60,
        "reckon crew dispatch --node review-of-source-node",
    )
    monkeypatch.setattr(obligations_module, "_classified_rows", lambda _project: [row])
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 2}
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    assert result["obligations"] == []
