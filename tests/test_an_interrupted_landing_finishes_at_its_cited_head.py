"""A landing refused after its promotion is finished at the head it cited.

``crew complete --land`` promotes first and merges afterwards, and writes a
pending-landing record before it promotes so a killed process still leaves a
landing to finish. That record used to name the run and its pointer but not the
revision the landing cited, so the pass that finished it read the head from
whatever the ledger row and live pointer had become. A resume or a repair round
moves both: the run reaches a head no review read, and the pass lands that head
— or holds the run ``awaiting-review`` on it — instead of merging the reviewed
revision the landing was asked for.

The record now keeps the cited head, and these cases hold the pass to it: the
cited head is merged, a later head is never merged through the record or held
against it, and a cited head already on the primary branch clears the record
with nothing merged.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import merge, pending_landing, queue_landing
from reckon.crew.runs import _write_json, pointer_path, read_pointer
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

RUN_ID = "r-20261113T070000000000-a-refused-landing-writes-nothing"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"
MARKER = "the reviewed change\n"


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


def _records() -> list[Path]:
    return sorted(pending_landing.pending_dir().glob("*.json"))


def _pointer(run: dict[str, Any], attempt: int, phase: str) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(run["repository"]),
        "worktree": str(run["worktree"]),
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "attempt": attempt,
        "phase": phase,
        "created_at": "2026-11-13T07:00:00Z",
        "base_sha": run["base"],
        "manifest_path": str(run["manifest"]),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "s12",
            "time_budget": "60m",
            "write_paths": ["reviewed.txt"],
        },
    }


@pytest.fixture()
def refused_landing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A ``--land`` whose promotion committed and whose merge step then failed."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktrees" / "refused-landing"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text(MARKER, encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "feat: add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: node-a\nstatus: complete\ncommits: {head}\n", encoding="utf-8"
    )
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    run: dict[str, Any] = {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "manifest": manifest,
        "base": _git(repository, "rev-parse", "HEAD"),
        "run_id": RUN_ID,
        "head": head,
        "gate_log": gate_log,
    }
    _write_json(pointer_path(RUN_ID), _pointer(run, 3, "delivered"))

    # The promotion runs and commits; the merge step then fails, which is the
    # refused landing this node is about. The failure is applied once so the
    # landing pass that runs afterwards merges for real.
    state = {"fail": True}
    real_land = merge.land

    def fail_the_land_step_once(*args: Any, **kwargs: Any) -> Any:
        if state["fail"]:
            state["fail"] = False
            raise merge.MergeError("a held index.lock refuses the merge")
        return real_land(*args, **kwargs)

    monkeypatch.setattr(merge, "land", fail_the_land_step_once)
    result = _complete_land(run)
    assert result.exit_code != 0, result.output
    assert _records(), "the refused landing wrote no pending-landing record"
    return run


def _resume_at_a_later_head(run: dict[str, Any]) -> str:
    """Move the run to a head no review read, as a repair round's resume does.

    Both stores the pass used to read move with it: the worktree and the live
    pointer hold the later head, and the ledger row is rewritten to it as a
    re-promotion would write it.
    """
    worktree = run["worktree"]
    (worktree / "later.txt").write_text("a change no review read\n", encoding="utf-8")
    _git(worktree, "add", "later.txt")
    _git(worktree, "commit", "-q", "-m", "feat: a later change")
    later = _git(worktree, "rev-parse", "HEAD")
    run["manifest"].write_text(
        f"node: node-a\nstatus: complete\ncommits: {later}\n", encoding="utf-8"
    )
    _write_json(pointer_path(RUN_ID), _pointer(run, 4, "delivered"))

    path = ledger.run_path(PROJECT, RUN_ID, run["repository"])
    row = json.loads(path.read_text(encoding="utf-8"))
    row["promoted_revision"] = later
    row["commits"] = [*(row.get("commits") or ()), later]
    ledger.write_run_file(PROJECT, row, run["repository"])
    return later


def test_the_pass_merges_the_cited_head_not_a_later_one(
    refused_landing: dict[str, Any],
) -> None:
    run = refused_landing
    later = _resume_at_a_later_head(run)

    results = pending_landing.finish_pending_landings()

    assert [row.get("status") for row in results] == ["finished"], results
    assert results[0]["head"] == run["head"]
    assert _is_ancestor(run["origin"], run["head"], "main"), (
        "the cited head never landed"
    )
    assert not _is_ancestor(run["origin"], later, "main"), (
        "a head the run reached after the citation was landed through the record"
    )
    assert not _records(), "the finished landing left its record behind"


def test_a_cited_head_merged_by_hand_is_cleared_with_nothing_merged(
    refused_landing: dict[str, Any],
) -> None:
    run = refused_landing
    later = _resume_at_a_later_head(run)
    _git(
        run["repository"], "merge", "-q", "--no-ff", "-m", "merge by hand", run["head"]
    )
    _git(run["repository"], "push", "-q", "origin", "main")
    hand_merged = _git(run["repository"], "rev-parse", "HEAD")

    results = pending_landing.finish_pending_landings()

    assert [row.get("status") for row in results] == ["already-landed"], results
    assert _git(run["repository"], "rev-parse", "HEAD") == hand_merged
    assert not _is_ancestor(run["origin"], later, "main"), (
        "the pass merged a later head over a landing already on the branch"
    )
    assert not _records(), "the settled landing left its record behind"


def test_a_promoted_run_is_never_held_awaiting_review_at_a_later_head(
    refused_landing: dict[str, Any],
) -> None:
    run = refused_landing
    later = _resume_at_a_later_head(run)

    queue_landing.land_once()

    held = (
        read_pointer(RUN_ID).get("admission_landing")
        if pointer_path(RUN_ID).exists()
        else None
    )
    assert (held or {}).get("head") != later, (
        f"the lander held the run awaiting-review at a head no review read: {held}"
    )
    assert _is_ancestor(run["origin"], run["head"], "main"), (
        "the cited head never landed"
    )
    assert not _is_ancestor(run["origin"], later, "main")
    assert not _records(), "the finished landing left its record behind"
