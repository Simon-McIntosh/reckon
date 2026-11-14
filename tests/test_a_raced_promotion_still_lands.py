"""A promotion that loses the ledger-row race still lands the reviewed head.

Two writers can promote one implementation at once: a coordinator's
``crew complete --land`` and the station's own promotion of the same run. One
writes the run's ledger row first; the other reaches the row already present.
The row is the run's promotion and neither writer may promote it twice, but the
head it names still has to reach the primary branch. Treating the other writer's
row as a refusal left the run delivered with its reviewed head off the branch
and its live pointer marked for a repair pass that had nothing to repair.

The race is set up by writing and committing the row the winning promotion
writes, which is the state the second promotion finds. The station marks the
pointer for promotion while that second promotion runs, so the pointer carries a
phase the promotion did not read without any newer attempt having started the
run. The second promotion retires that pointer and then merges the head its own
``--land`` carries.

A temporary repository with a bare origin stands in for the project, and a
temporary crew home for the station's state; the real pointer directory is
asserted untouched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import pending_landing, promotion, promotion_requests
from reckon.crew.runs import _write_json, pointer_path, read_pointer
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

MARKER = "the reviewed change survives the raced landing\n"
RUN_ID = "r-20261114T094542374628-a-raced-promotion"
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
def raced_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktrees" / "raced"
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
            "created_at": "2026-11-14T09:45:42Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s11",
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


def _the_other_writer_promotes_the_run(run: dict[str, Any]) -> None:
    """Write and commit the run's ledger row, as the winning promotion does."""
    ledger.append_run(
        PROJECT,
        {
            "run_id": run["run_id"],
            "project": PROJECT,
            "outcome": "the other writer promoted the run",
            "gate": "passed",
            "completed_at": "2026-11-14T09:50:00Z",
            "commits": [run["head"]],
            "promoted_revision": run["head"],
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s11",
                "write_paths": ["reviewed.txt"],
            },
            "session": {},
            "worktree": str(run["worktree"]),
            "repo": str(run["repository"]),
        },
        root=run["repository"],
    )
    _git(run["repository"], "add", "docs")
    _git(run["repository"], "commit", "-q", "-m", "promote: the other writer's row")


def _the_station_marks_the_pointer_while_the_promotion_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_commit = promotion._commit_landing_writes

    def commit_then_mark(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        promotion_requests._mark_pointer(
            {"run_id": RUN_ID}, promotion_requests.REQUESTED
        )
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_mark)


def test_the_losing_promotion_still_returns_and_lands_the_head(
    raced_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = raced_run
    _the_other_writer_promotes_the_run(run)
    assert not _is_ancestor(run["origin"], run["head"], "main"), (
        "the case must start with the reviewed head off the branch"
    )
    _the_station_marks_the_pointer_while_the_promotion_runs(monkeypatch)

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    assert _is_ancestor(run["origin"], run["head"], "main"), "head never landed"
    assert not pointer_path(run["run_id"]).exists(), "the pointer stayed held"
    assert not (pending_landing.pending_dir() / f"{run['run_id']}.json").exists(), (
        "the finished landing left its record behind"
    )


def test_an_attempt_a_resume_started_keeps_its_pointer(
    raced_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer a newer attempt wrote belongs to the run that attempt is doing."""
    run = raced_run
    _the_other_writer_promotes_the_run(run)
    real_commit = promotion._commit_landing_writes

    def commit_then_resume(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        current = read_pointer(RUN_ID)
        current["attempt"] = int(current.get("attempt") or 3) + 1
        current["phase"] = "running"
        _write_json(pointer_path(RUN_ID), current)
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_resume)

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    assert _is_ancestor(run["origin"], run["head"], "main"), "head never landed"
    assert read_pointer(run["run_id"])["attempt"] == 4
