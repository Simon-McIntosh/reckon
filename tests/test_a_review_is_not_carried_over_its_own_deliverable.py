"""A review is not carried onto the run's own deliverable or a finding's file.

A stored review is evidence about a revision, and carrying it to a new head is
sound only when the move leaves the reviewed work alone. A commit that changes a
file the reviewed diff changed, or one a stored finding cites, is new work: a
reviewer has not read it, so re-stamping the stored verdict onto the new head
reports a review of code no reviewer saw. A commit that only adds a record or a
log off to the side is not new work, and carrying the review over it is correct.

Every case drives the reflex decision end to end through
``recovery.carry_review_forward`` against a real git history, so the changed
paths are measured rather than supplied.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module

PROJECT = "not-carried-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", *paths], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository whose reviewed run delivered a test file, plus a config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    base = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "tests/test_cited.py": "def test_cited():\n    assert True\n",
        },
        "feat: the run's baseline",
    )
    reviewed = _commit(
        repo,
        {"tests/test_deliverable.py": "def test_delivered():\n    assert True\n"},
        "feat: the reviewed deliverable",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    return {"config_home": config_home, "repo": repo, "base": base, "reviewed": reviewed}


def _write_run(world: dict, *, run_id: str, head: str) -> None:
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repo),
            "worktree": str(repo),
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {"id": run_id, "plan": "fixture-plan", "write_paths": ["pkg/"]},
        },
    )


def _store_review(
    run_id: str, *, base: str, head: str, findings: list[dict] | None = None
) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    stored = review_module.parse_review(emitted)
    stored.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
            "findings": list(findings or ()),
        }
    )
    review_module.store_review(stored)


def _reviewed_run(world: dict, run_id: str) -> None:
    _write_run(world, run_id=run_id, head=world["reviewed"])
    _store_review(
        run_id,
        base=world["base"],
        head=world["reviewed"],
        findings=[
            {
                "file": "tests/test_cited.py",
                "line": "3",
                "text": "the cited test asserts nothing",  # noqa: RUF003
            }
        ],
    )


def test_a_move_over_a_test_file_the_reviewed_diff_changed_is_not_carried(world):
    repo = world["repo"]
    run_id = "r-deliverable-move"
    _reviewed_run(world, run_id)
    _commit(
        repo,
        {"tests/test_deliverable.py": "def test_delivered():\n    assert 1\n"},
        "fix: repair the delivered test",
    )
    record = runs.read_pointer(run_id)

    report = recovery.carry_review_forward(record)

    assert report is not None
    assert report["carried"] is False
    assert report["deliverable_paths"] == ["tests/test_deliverable.py"]
    assert (
        review_module.read_review(PROJECT, run_id, reviewed_head_sha=report["head"])
        is None
    )
    pointer = runs.read_pointer(run_id)
    declined = pointer[recovery.CARRY_FORWARD_FIELD]
    assert declined["carried"] is False
    assert declined["deliverable_paths"] == ["tests/test_deliverable.py"]


def test_a_move_over_a_path_a_finding_cites_is_not_carried(world):
    repo = world["repo"]
    run_id = "r-cited-move"
    _reviewed_run(world, run_id)
    _commit(
        repo,
        {"tests/test_cited.py": "def test_cited():\n    assert 1\n"},
        "fix: repair the cited test",
    )
    record = runs.read_pointer(run_id)

    report = recovery.carry_review_forward(record)

    assert report is not None
    assert report["carried"] is False
    assert report["deliverable_paths"] == ["tests/test_cited.py"]
    assert (
        review_module.read_review(PROJECT, run_id, reviewed_head_sha=report["head"])
        is None
    )


def test_a_move_over_an_unrelated_record_is_still_carried(world):
    repo = world["repo"]
    run_id = "r-record-move"
    _reviewed_run(world, run_id)
    new_head = _commit(
        repo,
        {"data/record.txt": "payload\n"},
        "chore(data): land a record",
    )
    record = runs.read_pointer(run_id)

    report = recovery.carry_review_forward(record)

    assert report is not None
    assert report["carried"] is True
    assert report["scope"] == ["data/record.txt"]
    carried = review_module.read_review(PROJECT, run_id, reviewed_head_sha=new_head)
    assert carried is not None
    assert recovery.same_revision(carried["reviewed_head_sha"], new_head)


def test_a_move_over_runtime_source_still_earns_the_light_rereview(world):
    repo = world["repo"]
    run_id = "r-source-move"
    _reviewed_run(world, run_id)
    _commit(repo, {"pkg/mod.py": "VALUE = 2\n"}, "fix: a later source commit")
    record = runs.read_pointer(run_id)

    report = recovery.carry_review_forward(record)

    assert report is not None
    assert report["carried"] is False
    assert report["review_tier"] == "light"
    assert report["scope"] == ["pkg/mod.py"]