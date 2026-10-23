"""A disposition recorded after promotion reaches the reflex that reads it.

A review run is promoted, and the coordinator then records the dispositions of
its findings on the committed record. The staging copy the host store holds was
last written before those dispositions, so a reader that returns the staging
record unmerged disagrees with the committed copy about the same review: the
reflex that composes the next round sees a blocking finding with no disposition
about which the committed record already holds the answer.

``stored_records_for_run`` enumerates the staging store, and each record it
returns now carries the answer fields — the finding and dimension dispositions,
the responses and the response events — held by every copy of the same review
run, staging and committed, merged through the store's own answer merge. These
cases stand up a review whose disposition lives on one copy and is read back
through another: the merged reader returns it, and the dispatch that refused a
round on the unmerged reading composes again.

Every store, repository and live pointer resolves inside a temporary
configuration home; nothing touches the operator's own store.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.recovery_review_dispatch")
repair_module = importlib.import_module("reckon.crew.repair")

PROJECT = "disposition-after-promotion"
SESSION = "coordinator-disposition"
PLAN = "disposition-plan"

REVIEWED = "r-20261009T100000000000-reviewed-subject"
REVIEW = "r-20261009T110000000000-review-run"
SECOND_REVIEW = "r-20261009T120000000000-second-review-run"
THIRD_REVIEW = "r-20261009T130000000000-third-review-run"
FOLD_NODE = "repair-the-disposed-finding"
FIRST_MTIME = 1_700_000_000_000_000_000
SECOND_MTIME = FIRST_MTIME + 1_000_000_000

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "fixture",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@dataclass(frozen=True)
class World:
    """The store and repository one assertion reads from."""

    home: Path
    repository: Path
    tree: Path
    reviewed_head: str
    later_head: str
    staging: Path
    committed: Path
    record: dict


def _finding(severity: str = "blocking") -> dict:
    """A run finding that declares a severity, with no id and so no answer.

    A finding carrying no id is resolved by its index and not by an answer that
    could name it, which is what makes the disposition recorded by index the
    only thing that lifts the refusal.
    """
    return {
        "file": "reckon/crew/first.py",
        "line": "10",
        "text": "the first defect",
        "severity": severity,
    }


def _record(*, review_run_id: str, head: str, review_base: str) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED,
        "review_run_id": review_run_id,
        "reviewed_base_sha": review_base,
        "reviewed_head_sha": head,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "findings": [_finding()],
        "timestamp": "2026-10-09T11:00:00+00:00",
    }


def _write_pointer(reviewed: str, *, worktree: Path) -> None:
    runs._write_json(
        runs.pointer_path(reviewed),
        {
            "run_id": reviewed,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "worktree": str(worktree),
            "manifest_path": str(worktree / "manifest.md"),
            "node": {
                "id": reviewed,
                "plan": PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


@pytest.fixture()
def world(isolated_reckon_home: Path, tmp_path: Path, monkeypatch) -> World:
    """A promoted review whose committed copy carries a finding's disposition."""
    home = isolated_reckon_home
    monkeypatch.delenv("RECKON_FLIGHT_CONFIG", raising=False)
    monkeypatch.delenv("RECKON_WORKTREES", raising=False)
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)

    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed disposition fixture"),
    ):
        _git(repository, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )

    # The run's tree carries the reviewed head and a later head the run moved to
    # with its repair, which is the state the trace this closes was in.
    tree = tmp_path / "run-tree"
    tree.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(tree, *arguments)
    (tree / "work.txt").write_text("reviewed\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: reviewed revision")
    reviewed_head = _git(tree, "rev-parse", "HEAD")
    (tree / "work.txt").write_text("repaired\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: repair revision")
    later_head = _git(tree, "rev-parse", "HEAD")
    (tree / "manifest.md").write_text("node: reviewed\nstatus: complete\n")

    _write_pointer(REVIEWED, worktree=tree)

    # The staging copy carries the blocking finding and no disposition: it was
    # written before the coordinator recorded the dispositions, which landed on
    # the committed copy below.
    record = _record(
        review_run_id=REVIEW,
        head=reviewed_head,
        review_base=reviewed_head,
    )
    staging = _write_json(review_module.review_path(PROJECT, REVIEWED), record)

    committed_root = review_module.committed_review_root(PROJECT)
    assert committed_root is not None, "premise: the project's reviews tree mounts"
    committed = _write_json(
        review_module.review_path(
            PROJECT,
            REVIEWED,
            committed_root=committed_root,
            review_run_id=REVIEW,
        ),
        {
            **record,
            "findings": [_finding()],
            review_module.FINDING_DISPOSITIONS_KEY: {
                "0": {
                    "kind": "folded",
                    "node": FOLD_NODE,
                    "recorded_at": "2026-10-09T12:00:00+00:00",
                }
            },
        },
    )
    assert committed.is_file()

    return World(
        home=home,
        repository=repository,
        tree=tree,
        reviewed_head=reviewed_head,
        later_head=later_head,
        staging=staging,
        committed=committed,
        record=record,
    )


def test_a_committed_disposition_reaches_a_staging_blocking_finding(
    world: World,
) -> None:
    """The staging copy alone carries the blocking finding and no answer.

    The committed copy of the same review run carries the finding's disposition,
    so the record returned can only have taken it from the merge of every copy.
    """
    staging_raw = json.loads(world.staging.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY not in staging_raw, (
        "premise: the staging copy carries no disposition of its own"
    )
    committed_raw = json.loads(world.committed.read_text(encoding="utf-8"))
    assert (
        committed_raw[review_module.FINDING_DISPOSITIONS_KEY]["0"]["node"] == FOLD_NODE
    ), "premise: the committed copy holds the disposition"

    records = review_module.stored_records_for_run(PROJECT, REVIEWED)

    assert len(records) == 1, "one staging record was enumerated"
    _path, stored = records[0]
    disposition = stored[review_module.FINDING_DISPOSITIONS_KEY]["0"]
    assert disposition["kind"] == "folded"
    assert disposition["node"] == FOLD_NODE


def test_a_promoted_disposition_lifts_the_blocking_refusal(world: World) -> None:
    """The reflex composes a round once the committed disposition is read.

    The run has moved past the reviewed head with its repair, and the review's
    blocking finding is answered on the committed copy alone. Read unmerged, the
    refusal sees the finding open and refuses the round; read merged, it lifts.
    """
    record = runs.read_pointer(REVIEWED)
    assert record["run_id"] == REVIEWED

    refusal = dispatch_module._open_review_refusal(record, dry_run=True)

    assert refusal is None, refusal and refusal.get("reason")

    report = dispatch_module.dispatch_review_for_run(
        record, config=CONFIG, dry_run=True
    )

    assert "no disposition" not in str(report.get("reason") or ""), report.get("reason")
    assert not report.get("undisposed_findings")


def test_a_disposition_on_the_staging_copy_is_still_returned(world: World) -> None:
    """An answer recorded on the staging copy survives the merge.

    The staging copy is a merge source, so a disposition recorded there before
    any promotion is returned as it was — the merge takes answers from every
    copy, not only the committed one.
    """
    staging_raw = json.loads(world.staging.read_text(encoding="utf-8"))
    staging_raw[review_module.FINDING_DISPOSITIONS_KEY] = {
        "0": {
            "kind": "exempted",
            "reason": "answered on the staging copy",
            "recorded_at": "2026-10-09T13:00:00+00:00",
        }
    }
    world.staging.write_text(json.dumps(staging_raw), encoding="utf-8")

    _path, stored = review_module.stored_records_for_run(PROJECT, REVIEWED)[0]

    disposition = stored[review_module.FINDING_DISPOSITIONS_KEY]["0"]
    assert disposition["kind"] == "exempted"
    assert disposition["reason"] == "answered on the staging copy"


def test_records_with_no_answers_are_returned_unchanged_and_in_order(
    world: World,
) -> None:
    """Two rounds with no answer on any copy read exactly as they were written.

    The fixture's own review run is removed so neither record here has a
    committed copy to draw an answer from. The merge takes each record as its
    own review run's copies, so it adds nothing to a record that carries no
    answer, and the two come back in the store's mtime order — the order the
    unmerged reader gave.
    """
    world.staging.unlink()
    world.committed.unlink()

    first = _record(
        review_run_id=SECOND_REVIEW,
        head=world.later_head,
        review_base=world.reviewed_head,
    )
    first_path = _write_json(
        review_module.review_path(
            PROJECT, REVIEWED, reviewed_head_sha=world.later_head
        ),
        first,
    )
    second = _record(
        review_run_id=THIRD_REVIEW,
        head=world.later_head,
        review_base=world.reviewed_head,
    )
    second_path = _write_json(
        review_module.review_path(PROJECT, REVIEWED),
        second,
    )
    os.utime(first_path, (0, FIRST_MTIME / 1e9))
    os.utime(second_path, (0, SECOND_MTIME / 1e9))

    records = review_module.stored_records_for_run(PROJECT, REVIEWED)

    paths = [path for path, _record in records]
    raw = [json.loads(path.read_text(encoding="utf-8")) for path in paths]

    assert paths == [first_path, second_path], "the store's mtime order is kept"
    assert [dict(record) for _path, record in records] == raw, (
        "a record with no answer on any copy is returned unchanged"
    )
    assert not any(
        review_module.FINDING_DISPOSITIONS_KEY in record for _path, record in records
    ), "no answer is invented for a record that carries none"
    assert repair_module.blocking_finding_dispositions(raw[0]), (
        "premise: the first record still owes an open blocking finding unmerged"
    )
