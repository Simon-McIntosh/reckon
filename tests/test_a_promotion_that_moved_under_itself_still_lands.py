"""A --land promotion whose pointer moves under it still lands its reviewed head.

A ``crew complete --land`` promotes first: it appends the ledger row, commits the
plan comment and the row, then removes the live pointer and merges the reviewed
head. A resume that starts the same run again between those moments moves the
pointer's attempt, so the removal guard refuses, the merge never runs, and the
branch does not carry the change the row says landed.

The promotion keeps the pointer it could not remove and marks it, and the landing
pass finishes the run from the record ``--land`` wrote before it promoted. Two
states have to end with the pointer released: the pass merging the head, and a
head a session merged by hand before the pass ran, which the pass must recognise
rather than merging a second time.

A temporary repository with a bare origin stands in for the project, a temporary
crew home for the crew state, and the real pointer directory is asserted
untouched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import pending_landing, promotion
from reckon.crew.runs import _write_json, pointer_path, read_pointer
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

MARKER = "the reviewed change survives the merge\n"
RUN_ID = "r-20261112T175326514846-a-promotion-that-moved"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"


def _is_ancestor(repository: Path, revision: str, branch: str) -> bool:
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                revision,
                branch,
            ],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.fixture(autouse=True)
def the_real_pointer_directory_is_not_a_fixture_target() -> None:
    assert not (REAL_LIVE / f"{RUN_ID}.json").exists()
    yield
    assert not (REAL_LIVE / f"{RUN_ID}.json").exists()


@pytest.fixture()
def moved_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A finished run whose worktree holds one committed change, ready to land."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktrees" / "moved-under-itself"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text(MARKER, encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "attempt": 3,
            "phase": "delivered",
            "created_at": "2026-11-12T17:53:26Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s7",
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": RUN_ID,
        "head": head,
        "gate_log": gate_log,
    }


def _move_the_pointer_while_promotion_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the run again after the landing writes commit, as a resume does."""
    real_commit = promotion._commit_landing_writes

    def commit_then_resume(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        current = read_pointer(RUN_ID)
        current["attempt"] = int(current.get("attempt") or 3) + 1
        current["phase"] = "running"
        _write_json(pointer_path(RUN_ID), current)
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_resume)


def _records() -> list[Path]:
    return sorted(pending_landing.pending_dir().glob("*.json"))


def _refused_landing(run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Land the run under a pointer that moves, and assert the refusal was recorded."""
    _move_the_pointer_while_promotion_runs(monkeypatch)
    result = _complete_land(run)
    assert result.exit_code != 0, result.output
    assert "changed while promotion ran" in result.output, result.output
    kept = read_pointer(run["run_id"])
    assert kept.get("pending_landing_repair") is True, kept
    assert pointer_path(run["run_id"]).exists()
    assert _records(), "the landing pass never learned the run was being landed"


def test_the_next_pass_lands_the_head_the_promotion_could_not_merge(
    moved_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = moved_run
    _refused_landing(run, monkeypatch)

    results = pending_landing.finish_pending_landings()

    assert [row.get("status") for row in results] == ["finished"], results
    assert results[0]["head"] == run["head"]
    assert _is_ancestor(run["origin"], run["head"], "main"), "head never landed"
    assert not pointer_path(run["run_id"]).exists(), "the pointer stayed held"
    assert not _records(), "the finished landing left its record behind"


def test_a_head_merged_by_hand_is_recognised_without_a_second_merge(
    moved_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = moved_run
    _refused_landing(run, monkeypatch)
    _git(
        run["repository"], "merge", "-q", "--no-ff", "-m", "merge by hand", run["head"]
    )
    _git(run["repository"], "push", "-q", "origin", "main")
    hand_merged = _git(run["repository"], "rev-parse", "HEAD")

    results = pending_landing.finish_pending_landings()

    assert [row.get("status") for row in results] == ["already-landed"], results
    assert _git(run["repository"], "rev-parse", "HEAD") == hand_merged
    assert _is_ancestor(run["origin"], run["head"], "main")
    assert not pointer_path(run["run_id"]).exists(), "the pointer stayed held"
    assert not _records()


def test_a_pointer_that_lost_its_mark_still_owes_the_promoted_merge(
    moved_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed attempt rewrote the pointer and dropped the mark.

    The promoted row still owes the merge, and the attempt that rewrote the
    pointer cannot land it: a promoted run has nothing left to promote. Reading
    the mark as the only evidence of an owed landing left the reviewed head off
    the branch with the pointer held.
    """
    run = moved_run
    _refused_landing(run, monkeypatch)
    current = read_pointer(run["run_id"])
    current["attempt"] = int(current["attempt"]) + 1
    current["phase"] = "running"
    for key in ("pending_landing_repair", "pending_landing_repair_attempt"):
        current.pop(key, None)
    _write_json(pointer_path(run["run_id"]), current)

    results = pending_landing.finish_pending_landings()

    assert [row.get("status") for row in results] == ["finished"], results
    assert _is_ancestor(run["origin"], run["head"], "main"), "head never landed"
    assert not _records(), "the finished landing left its record behind"
    # The pointer belongs to the attempt that rewrote it, and that attempt's
    # worker is not this landing's to remove.
    assert read_pointer(run["run_id"])["attempt"] == current["attempt"]
