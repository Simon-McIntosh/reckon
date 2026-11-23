"""A merged run is recorded from its run directory when its pointer is gone.

A reviewed head can reach the primary branch before any promotion merges it,
and the run's live pointer can be removed before any promotion or discard
records the run. The run directory survives that removal: the supervisor names
the repository and worktree, the dispatch prompt names the plan section, and
the durable manifest names the head the run delivered. ``crew record-merge``
records such a run from that directory, writing the ledger row that names the
merge and releasing the requests waiting on the section the run delivered.

A merge that does not carry the run's recorded head is refused naming both
commits, and a run the ledger already holds is refused as already recorded, so
a second recording cannot write a second row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew import queue_admission
from reckon.crew import review as review_module
from reckon.crew.open_run_index import row_for_run
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _write_origin_and_checkout,
)

SECTION = "§4"


@pytest.fixture()
def pointerless_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A reviewed run whose pointer is gone and whose head main carries."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    run_id = "r-20261123T090000000000-a-merged-run"
    worktree = tmp_path / "worktrees" / "merged"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text("the reviewed change\n", encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    base = _git(repository, "rev-parse", "HEAD")

    directory = run_dir(run_id)
    directory.mkdir(parents=True)
    gate_log = directory / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    (directory / "supervisor.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo": str(repository),
                "worktree": str(worktree),
                "fenced": False,
            }
        ),
        encoding="utf-8",
    )
    (directory / "prompt.txt").write_text(
        "You are a worker on one node.\n"
        "\n"
        "NODE     node-a\n"
        "GOAL     deliver the reviewed change\n"
        f"PLAN     {PROJECT}:{PLAN} {SECTION}\n",
        encoding="utf-8",
    )
    (directory / "attempt.json").write_text(
        json.dumps({"attempt": 1, "attempt_started_at": "2026-11-23T09:00:00Z"}),
        encoding="utf-8",
    )
    manifest = directory / "manifest.md"
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {head}\n"
        "changed_paths: reviewed.txt\n"
        "tests: true\n"
        f"test_logs: {gate_log}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        pointer_record(run_id, repository, worktree, manifest, base),
    )
    stored_review(run_id)
    # The merge that carried the run's head released its worktree, and the
    # pointer was removed before any promotion or discard recorded the run. Only
    # the run directory is left to answer for the run.
    _git(repository, "worktree", "remove", "--force", str(worktree))
    pointer_path(run_id).unlink()
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": run_id,
        "directory": directory,
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
        "created_at": "2026-11-23T09:00:00Z",
        "base_sha": base,
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": SECTION,
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


def _merge_head(run: dict, message: str) -> str:
    _git(
        run["repository"],
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        message,
        run["head"],
    )
    merge_sha = _git(run["repository"], "rev-parse", "HEAD")
    _git(run["repository"], "push", "-q", "origin", "main")
    return merge_sha


def test_a_pointerless_merged_run_is_recorded_and_releases_its_waiters(
    pointerless_run: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row names the merge and the section's waiters are released."""
    run = pointerless_run
    merge_sha = _merge_head(run, "merge the reviewed change")
    assert not pointer_path(run["run_id"]).exists(), "the fixture removes the pointer"
    released: list[tuple[list[str], str]] = []

    def _release(refs, *, project: str = "", **_kwargs):
        released.append((list(refs), project))
        return {
            "candidates": ["r-waiter"],
            "promoted": ["r-waiter"],
            "blocked": [],
            "failed": {},
        }

    monkeypatch.setattr(queue_admission, "_release_waiters", _release)

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
    assert payload["released"]["candidates"] == ["r-waiter"]
    assert released == [([f"{PLAN}#{SECTION}"], PROJECT)]
    assert row_for_run(PROJECT, run["run_id"], run["repository"]) is not None


def test_a_merge_that_does_not_carry_the_head_is_refused(
    pointerless_run: dict,
) -> None:
    """A merge of something else names both commits and records nothing."""
    run = pointerless_run
    (run["repository"] / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(run["repository"], "add", "unrelated.txt")
    _git(run["repository"], "commit", "-q", "-m", "an unrelated change")
    unrelated = _git(run["repository"], "rev-parse", "HEAD")

    result = _record_merge(run, unrelated)

    assert result.exit_code != 0, result.output
    assert run["head"] in result.output
    assert unrelated in result.output
    assert row_for_run(PROJECT, run["run_id"], run["repository"]) is None


def test_a_run_the_ledger_already_holds_is_refused(
    pointerless_run: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second recording of one run writes no second row."""
    run = pointerless_run
    merge_sha = _merge_head(run, "merge the reviewed change")
    monkeypatch.setattr(
        queue_admission,
        "_release_waiters",
        lambda *_args, **_kwargs: {
            "candidates": [],
            "promoted": [],
            "blocked": [],
            "failed": {},
        },
    )
    first = _record_merge(run, merge_sha)
    assert first.exit_code == 0, first.output

    second = _record_merge(run, merge_sha)

    assert second.exit_code != 0, second.output
    assert "already recorded" in second.output
    rows = list(ledger.runs(PROJECT, root=run["repository"]))
    assert [row["run_id"] for row in rows].count(run["run_id"]) == 1
