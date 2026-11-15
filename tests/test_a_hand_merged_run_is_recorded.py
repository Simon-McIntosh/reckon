"""A run the lead merged by hand is recorded, not left live.

A reviewed head can reach the primary branch before any promotion merges it.
When that happens the run it belongs to is left live: its pointer, worktree and
claims stay held, the section it delivered stays blocked, and a pending landing
record can resurrect the pointer after it is retired. Recording the merge is
therefore a step of its own: the row names the merge that carried the run, the
run is released as a landing would release it, and no suite delta is measured
because the branch already carries the change.

A merge that does not carry the run's reviewed head is refused, and the run is
left exactly as it was so the coordinator can record the right commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew import pending_landing
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _write_origin_and_checkout,
)


@pytest.fixture()
def merged_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A reviewed run whose head the primary branch already carries."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    run_id = "r-20261115T100000000000-a-hand-merged-run"
    worktree = tmp_path / "worktrees" / "hand-merged"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text("the reviewed change\n", encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    base = _git(repository, "rev-parse", "HEAD")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {head}\n"
        "tests: true\n"
        f"test_logs: {gate_log}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        pointer_record(run_id, repository, worktree, manifest, base),
    )
    stored_review(run_id)
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": run_id,
        "head": head,
        "base": base,
        "manifest": manifest,
    }


def pointer_record(run_id, repository, worktree, manifest, base) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(worktree),
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-11-15T10:00:00Z",
        "base_sha": base,
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "§4",
            "time_budget": "35m",
            "write_paths": ["reviewed.txt"],
        },
    }


def stored_review(run_id: str) -> None:
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


def _record_merge(run: dict, merge_sha: str):
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "record-merge",
            "--run",
            run["run_id"],
            "--merge",
            merge_sha,
            "--reason",
            "the lead merged the reviewed head by hand",
            "--checkout-path",
            str(run["repository"]),
            "--no-impl-change",
            "the fixture lands one file and moves no plan",
        ],
    )


def test_a_hand_merged_run_is_recorded_on_the_branch(merged_run: dict) -> None:
    """The row names the merge and the run stops being live."""
    run = merged_run
    _git(
        run["repository"],
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        "merge the reviewed change",
        run["head"],
    )
    merge_sha = _git(run["repository"], "rev-parse", "HEAD")
    _git(run["repository"], "push", "-q", "origin", "main")
    pointer_before = pointer_path(run["run_id"])
    assert pointer_before.exists()
    pending_landing.write_pending(
        run["run_id"],
        PROJECT,
        pointer=json.loads(pointer_before.read_text()),
        head=run["head"],
    )

    result = _record_merge(run, merge_sha)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True, payload
    assert payload["recorded_merge"] == merge_sha
    row = json.loads(
        ledger.run_path(PROJECT, run["run_id"], root=run["repository"]).read_text()
    )
    assert merge_sha in row["commits"]
    assert row["suite_delta"]["status"] == "not-run"
    assert row["suite_delta"]["recorded_merge"] == merge_sha
    assert not pointer_before.exists()
    assert not pending_landing._record_path(run["run_id"]).exists()


def test_a_merge_that_does_not_carry_the_head_is_refused(merged_run: dict) -> None:
    """A merge of something else leaves the run live."""
    run = merged_run
    (run["repository"] / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(run["repository"], "add", "unrelated.txt")
    _git(run["repository"], "commit", "-q", "-m", "an unrelated change")
    other_sha = _git(run["repository"], "rev-parse", "HEAD")

    result = _record_merge(run, other_sha)

    assert result.exit_code != 0, result.output
    assert run["head"] in result.output
    assert pointer_path(run["run_id"]).exists()
