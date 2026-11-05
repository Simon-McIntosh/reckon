"""A disposition names the review it writes.

``crew dispose`` answers one finding of a run's review by index. A run reviewed
again after a repair carries more than one round: an earlier round may already
be promoted into the project's committed tree, while the newer one has been
delivered and awaits promotion in the staging store. Selection by the run's
current head, and its fallback to a review of an ancestor head, can both land
on the earlier round — the descent searches only the committed tree, so a newer
round still awaiting promotion is invisible to it. A disposition written that
way retires a finding on a review the caller was not reading, and the receipt's
path was the only sign of it.

This module drives the command through its CLI entry point with a temporary
crew home and repository, and asserts three things: a call naming no review is
refused when the record it selected is not the run's newest delivered review,
and the refusal names that review run; the same call naming the newer review
writes onto that review's record alone; and every successful receipt names the
review run and the head it read. The earlier record's bytes are compared before
and after each call, because a write onto the wrong record is otherwise
indistinguishable from a correct one at every later read.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "disposition-names-its-review-fixture"
SESSION = "coordinator-fixture"
PLAN = "disposition-names-its-review-plan"

REVIEWED_RUN = "r-20261010T090000000000-reviewed-run"
EARLIER_REVIEW = "r-20261010T100000000000-review-of-the-earlier-head"
NEWER_REVIEW = "r-20261010T110000000000-review-of-the-current-head"

FOLD_NODE = "fix-a"


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    """The temporary world one assertion reads its report from."""

    config_home: Path
    repository: Path
    earlier_head: str
    newer_head: str
    earlier_record: Path
    newer_record: Path


def _findings() -> list[dict[str, str]]:
    """Two findings, in the order the findings reader prints them."""
    return [
        {
            "file": "reckon/crew/review.py",
            "line": "10",
            "text": "The guard is reachable with an empty index.",
            "severity": "follow-on",
        },
        {
            "file": "reckon/crew/review.py",
            "line": "20",
            "text": "The refusal names the wrong field.",
            "severity": "follow-on",
        },
    ]


def _review_record(
    review_run_id: str, *, base_sha: str, head_sha: str
) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": review_run_id,
        "status": "parsed",
        "reviewed_base_sha": base_sha,
        "reviewed_head_sha": head_sha,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "findings": _findings(),
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }


def _write_pointer(run_id: str, *, worktree: Path) -> None:
    """Write the live pointer naming the tree the reviewed run's work sits in."""
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "repo": str(worktree),
            "worktree": str(worktree),
            "node": {
                "id": run_id,
                "plan": PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["docs"],
            },
        },
    )


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Fixture:
    """A run whose work has moved past both of its reviews.

    The earlier review is promoted into the project's committed tree, and the
    newer one is delivered into the staging store and not yet promoted. The
    run's worktree head is a later revision than either, which is the state a
    repair leaves: selection by the head finds no record, and the descent from
    it reaches only the committed round.
    """
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    for index in (1, 2, 3):
        (repository / f"seed-{index}.txt").write_text(f"{index}\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repository, *arguments)
    for index in (1, 2, 3):
        _git(repository, "add", f"seed-{index}.txt")
        _git(repository, "commit", "-q", "-m", f"test: seed revision {index}")
    earlier_head = _git(repository, "rev-parse", "HEAD~2")
    newer_head = _git(repository, "rev-parse", "HEAD~1")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )

    _write_pointer(REVIEWED_RUN, worktree=repository)
    earlier_record = review_module.store_committed_review(
        _review_record(EARLIER_REVIEW, base_sha="a" * 40, head_sha=earlier_head),
        root=repository,
    )
    newer_record = review_module.store_review(
        _review_record(NEWER_REVIEW, base_sha=earlier_head, head_sha=newer_head)
    )
    return Fixture(
        config_home=isolated_reckon_home,
        repository=repository,
        earlier_head=earlier_head,
        newer_head=newer_head,
        earlier_record=earlier_record,
        newer_record=newer_record,
    )


def _dispose(fixture: Fixture, *arguments: str):
    """Invoke the disposition command for the fixture run through the CLI."""
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "dispose",
            "--project",
            PROJECT,
            "--run",
            REVIEWED_RUN,
            *arguments,
        ],
    )


def _recorded_dispositions(path: Path) -> dict[str, Any]:
    stored = json.loads(path.read_text(encoding="utf-8"))
    return stored.get(review_module.FINDING_DISPOSITIONS_KEY) or {}


def test_a_call_naming_no_review_is_refused_when_an_older_round_was_selected(
    fixture: Fixture,
) -> None:
    """The refusal names the newer review run, and neither record is written."""
    earlier_before = fixture.earlier_record.read_bytes()
    newer_before = fixture.newer_record.read_bytes()

    result = _dispose(
        fixture, "--finding", "0", "--kind", "folded", "--node", FOLD_NODE
    )

    assert result.exit_code != 0, result.output
    assert NEWER_REVIEW in result.output
    assert fixture.earlier_record.read_bytes() == earlier_before
    assert fixture.newer_record.read_bytes() == newer_before


def test_naming_the_newer_review_writes_onto_that_records_only(
    fixture: Fixture,
) -> None:
    """The named review's record carries the disposition; the earlier one does not."""
    earlier_before = fixture.earlier_record.read_bytes()

    result = _dispose(
        fixture,
        "--review",
        NEWER_REVIEW,
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )

    assert result.exit_code == 0, result.output
    assert _recorded_dispositions(fixture.newer_record)["0"]["kind"] == "folded"
    assert _recorded_dispositions(fixture.newer_record)["0"]["node"] == FOLD_NODE
    assert fixture.earlier_record.read_bytes() == earlier_before
    assert _recorded_dispositions(fixture.earlier_record) == {}


def test_every_successful_receipt_names_the_review_and_the_head_it_read(
    fixture: Fixture,
) -> None:
    """The receipt carries the record's review run id and the head it names."""
    result = _dispose(
        fixture,
        "--review",
        NEWER_REVIEW,
        "--finding",
        "1",
        "--kind",
        "exempted",
        "--reason",
        "the finding is answered by the merged head reviewed beside it",
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["review_run_id"] == NEWER_REVIEW
    assert receipt["reviewed_head"] == fixture.newer_head
    assert receipt["path"] == str(fixture.newer_record)


def test_a_named_review_the_run_never_carried_is_refused(fixture: Fixture) -> None:
    """``--review`` names a review run of this run; another id is refused."""
    earlier_before = fixture.earlier_record.read_bytes()

    result = _dispose(
        fixture,
        "--review",
        "r-20261010T120000000000-review-of-another-run",
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )

    assert result.exit_code != 0, result.output
    assert "review-of-another-run" in result.output
    assert fixture.earlier_record.read_bytes() == earlier_before
