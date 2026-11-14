"""A promotion that loses the ledger-row race still lands the reviewed head.

Two writers can promote one implementation in turn: one writes the run's ledger
row first, and the other reaches the row already present. The row is the run's
promotion and neither writer may promote it twice, so this node's fix is that
the second writer treats the row as its own success, merges the head the row
names, and retires its pointer. A promotion that raised instead left the run
delivered with its reviewed head off the branch and its live pointer marked for
a repair-only pass.

The first promotion here is a real ``crew complete --land --wait``, paused in
its own landing step once its ledger row is committed. The second is another
real ``crew complete --land --wait`` started at that point, so the pair is the
callers the defect was measured between rather than a hand-written row.

A temporary repository with a bare origin stands in for the project, and a
temporary crew home for the station's state; the real pointer directory is
asserted untouched.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from reckon import crew_run_commands
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
PAUSE_SECONDS = 60.0


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


def _pending_path(run_id: str) -> Path:
    return pending_landing.pending_dir() / f"{run_id}.json"


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
    manifest.write_text(
        f"node: node-a\nstatus: complete\ncommits: {head}\n", encoding="utf-8"
    )
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


def _the_first_promotion_pauses_in_its_landing(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Pause the first ``--land`` once its ledger row is committed.

    The pause sits in the landing step the call runs after promotion returns,
    so the run's row, its landing commit and its pointer removal have all
    happened and the promotion lock is free for the second caller.
    """
    promoted = threading.Event()
    release = threading.Event()
    real_land = crew_run_commands._land_a_promoted_run
    calls = {"count": 0}

    def land_pausing_once(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            promoted.set()
            if not release.wait(PAUSE_SECONDS):
                raise AssertionError("the second promotion never ran")
        return real_land(*args, **kwargs)

    monkeypatch.setattr(crew_run_commands, "_land_a_promoted_run", land_pausing_once)
    return promoted, release


def _a_resume_started_the_run(monkeypatch: pytest.MonkeyPatch, pointer: dict) -> dict:
    """Rewrite the live pointer as a newer attempt's as the promotion commits.

    The promotion reads the pointer when it starts, so a resume that begins
    while it runs is the case that matters: the attempt it holds is no longer
    the attempt the file names.
    """
    newer = {**pointer, "attempt": 4, "phase": "running"}
    newer.pop("pending_landing_repair", None)
    real_commit = promotion._commit_landing_writes

    def commit_then_resume(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        _write_json(pointer_path(RUN_ID), newer)
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_resume)
    return newer


def test_the_losing_promotion_returns_and_lands_the_head(
    raced_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second caller meets the first's row and still lands the head."""
    run = raced_run
    delivered = read_pointer(run["run_id"])
    promoted, release = _the_first_promotion_pauses_in_its_landing(monkeypatch)
    first: dict[str, Any] = {}

    def promote_first() -> None:
        first["result"] = _complete_land(run)

    thread = threading.Thread(target=promote_first, daemon=True)
    thread.start()
    assert promoted.wait(PAUSE_SECONDS), "the first promotion never wrote its row"
    assert not _is_ancestor(run["origin"], run["head"], "main"), (
        "the case must start with the reviewed head off the branch"
    )

    stranded = dict(delivered)
    stranded["phase"] = promotion_requests.REQUESTED
    stranded["promotion_status"] = promotion_requests.REQUESTED
    _write_json(pointer_path(run["run_id"]), stranded)

    second = _complete_land(run)
    assert second.exit_code == 0, second.output

    release.set()
    thread.join(PAUSE_SECONDS)
    assert not thread.is_alive(), "the first promotion never returned"
    assert first["result"].exit_code == 0, first["result"].output
    assert _is_ancestor(run["origin"], run["head"], "main"), "head never landed"
    assert not pointer_path(run["run_id"]).exists(), "the pointer stayed held"
    assert not _pending_path(run["run_id"]).exists(), (
        "the finished landing left its record behind"
    )


def test_an_attempt_a_resume_started_keeps_its_pointer(
    raced_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer a newer attempt wrote is left exactly as that attempt left it."""
    run = raced_run
    delivered = read_pointer(run["run_id"])
    promoted, release = _the_first_promotion_pauses_in_its_landing(monkeypatch)
    first: dict[str, Any] = {}

    def promote_first() -> None:
        first["result"] = _complete_land(run)

    thread = threading.Thread(target=promote_first, daemon=True)
    thread.start()
    assert promoted.wait(PAUSE_SECONDS), "the first promotion never wrote its row"

    stranded = dict(delivered)
    stranded["phase"] = promotion_requests.REQUESTED
    stranded["promotion_status"] = promotion_requests.REQUESTED
    _write_json(pointer_path(run["run_id"]), stranded)
    newer = _a_resume_started_the_run(monkeypatch, stranded)
    second = _complete_land(run)

    release.set()
    thread.join(PAUSE_SECONDS)
    assert second.exit_code == 0, second.output
    assert _is_ancestor(run["origin"], run["head"], "main"), "head never landed"
    kept = read_pointer(run["run_id"])
    assert kept == newer, "the newer attempt's pointer was rewritten"
    assert kept["attempt"] == 4, "the newer attempt lost its pointer"
    assert "pending_landing_repair" not in kept, (
        "the newer attempt was marked for a repair its next call cannot finish"
    )
    assert first["result"].exit_code == 0, first["result"].output


def test_the_record_cites_the_reviewed_head_and_never_replaces_one(
    raced_run: dict[str, Any],
) -> None:
    """The record names the head the promotion resolved before it wrote anything."""
    run = raced_run
    record = read_pointer(run["run_id"])
    (run["worktree"] / "later.txt").write_text("a later commit\n", encoding="utf-8")
    _git(run["worktree"], "add", "later.txt")
    _git(run["worktree"], "commit", "-q", "-m", "advance the tree after review")
    advanced = _git(run["worktree"], "rev-parse", "HEAD")
    assert advanced != run["head"], "the tree has to move for this case"

    promotion._record_a_raced_landing(run["run_id"], record, reviewed_head=run["head"])

    written = json.loads(_pending_path(run["run_id"]).read_text(encoding="utf-8"))
    assert written["head"] == run["head"], "the record cites a head no review covered"

    replaced = {**written, "head": advanced}
    _pending_path(run["run_id"]).write_text(json.dumps(replaced), encoding="utf-8")
    promotion._record_a_raced_landing(run["run_id"], record, reviewed_head=run["head"])
    kept = json.loads(_pending_path(run["run_id"]).read_text(encoding="utf-8"))
    assert kept["head"] == advanced, "a record another attempt wrote was replaced"
