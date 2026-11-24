"""A hand merge is recorded with the gate that actually passed.

A recorded suite arm can abort natively (exit 134) before a test file is split,
so the run's own arms are no longer evidence of anything. The gate that did
pass is the coordinator's merged-head run, logged elsewhere. ``crew merge``
takes that gate, but ``crew record-merge`` accepted none and ignored a declared
gate, so a run whose recorded arm aborted could not be recorded after a hand
merge even when its merged-head gate passed. The recording now accepts
``--gate-*`` evidence as ``crew merge`` does: a passing gate is recorded on the
row, a gate whose exit status is not zero is refused, and a run given no gate is
refused as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _write_origin_and_checkout,
)

ARMED_COMMAND = "pytest -q tests/"
GATE_COMMAND = "pytest -q tests/ -p no:cacheprovider"
GATE_LOG = "/durable/carriers-merged-gate-2.log"


def _aborted_arm(revision: str) -> dict:
    """An arm whose program aborted before a summary was printed."""
    return {
        "revision": revision,
        "command": ARMED_COMMAND,
        "exit_status": 134,
        "log_path": "/durable/" + revision + ".log",
        "completed": False,
        "failure_count": None,
        "failure_ids": [],
    }


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
    arm_log = tmp_path / "arm.log"
    arm_log.write_text("# command: pytest\nSubprocess aborted\n", encoding="utf-8")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {head}\n"
        f"tests: {ARMED_COMMAND}\n"
        f"test_logs: {arm_log}\n"
        f"baseline_suite: {json.dumps(_aborted_arm(base))}\n"
        f"after_suite: {json.dumps(_aborted_arm(head))}\n",
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
        "suite_command": ARMED_COMMAND,
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


def _record_merge(run: dict, merge_sha: str, *extra: str):
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
            *extra,
        ],
    )


def _merge_head(run: dict) -> str:
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
    return merge_sha


def _row(run: dict) -> dict:
    return json.loads(
        ledger.run_path(PROJECT, run["run_id"], root=run["repository"]).read_text()
    )


def test_an_aborted_arm_is_recorded_with_the_gate_that_passed(merged_run: dict) -> None:
    """Given a passing gate, the aborted arm no longer refuses the recording."""
    run = merged_run
    merge_sha = _merge_head(run)

    result = _record_merge(
        run,
        merge_sha,
        "--gate-command",
        GATE_COMMAND,
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        GATE_LOG,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True, payload
    assert payload["recorded_merge"] == merge_sha
    row = _row(run)
    assert row["gate_check"]["command"] == GATE_COMMAND
    assert row["gate_check"]["exit_status"] == 0
    assert row["gate_check"]["log_path"] == GATE_LOG
    assert not pointer_path(run["run_id"]).exists()


def test_a_gate_whose_exit_status_is_not_zero_is_refused(merged_run: dict) -> None:
    """A failing gate is not evidence and the run stays live."""
    run = merged_run
    merge_sha = _merge_head(run)

    result = _record_merge(
        run,
        merge_sha,
        "--gate-command",
        GATE_COMMAND,
        "--gate-exit-status",
        "1",
        "--gate-log-path",
        GATE_LOG,
    )

    assert result.exit_code != 0, result.output
    assert "exited zero" in result.output, result.output
    assert pointer_path(run["run_id"]).exists()


def test_a_run_given_no_gate_is_refused_as_before(merged_run: dict) -> None:
    """With no gate the recorded aborted arm still refuses, as it did before."""
    run = merged_run
    merge_sha = _merge_head(run)

    result = _record_merge(run, merge_sha)

    assert result.exit_code != 0, result.output
    assert "--waive-suite-delta" in result.output, result.output
    assert pointer_path(run["run_id"]).exists()
