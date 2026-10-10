"""A review finding carries a disposition, so a later review does not re-find it.

``crew dispose`` answers a review finding. Its first space is a sub-floor review
dimension; its second is one entry in the run's newest review record, named by
the index the findings reader prints. This module drives that second space
through the CLI entry point — a fold naming the node a finding went into, and an
exemption recording why it is not being acted on — and reads the disposition
back from the findings reader, the surface a coordinator's next review consults.
The command is exercised the way an operator calls it, so a call naming
``--finding`` exits as an unknown option at the revision before the option
existed, which is what makes this module fail there.

The refusals are asserted through the same entry point, because a refusal that
still wrote would be indistinguishable from an accepted call at every later
read: an exemption carrying no reason, a call naming both ``--dimension`` and
``--finding``, and a call naming neither each exit non-zero and leave the
record's bytes exactly as they were. Every crew directory is resolved inside a
temporary config home; nothing touches the operator's two stores.
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
from reckon.crew import findings as findings_module
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "dispose-finding-fixture"
SESSION = "coordinator-fixture"
PLAN = "dispose-finding-plan"

REVIEWED_RUN = "r-20261010T090000000000-reviewed-run"
REVIEW_RUN = "r-20261010T100000000000-review-run"
FOLD_NODE = "fix-a"

# The token the findings reader reports when a finding carries no disposition.
OPEN = "open"


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
    head_sha: str
    reviewed: str
    review_file: Path


def _findings() -> list[dict[str, str]]:
    """Two follow-on findings on one path, in the order the reader prints them."""
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


def _review_record(head_sha: str) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "status": "parsed",
        "reviewed_base_sha": "a" * 40,
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
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed finding-disposition fixture"),
    ):
        _git(repository, *arguments)
    head_sha = _git(repository, "rev-parse", "HEAD")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )

    _write_pointer(REVIEWED_RUN, worktree=repository)
    review_file = review_module.store_committed_review(
        _review_record(head_sha), root=repository
    )
    return Fixture(
        config_home=isolated_reckon_home,
        repository=repository,
        head_sha=head_sha,
        reviewed=REVIEWED_RUN,
        review_file=review_file,
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
            fixture.reviewed,
            *arguments,
        ],
    )


def _by_index(fixture: Fixture) -> dict[int, str]:
    """The disposition of each finding, by index, as the findings reader reports it."""
    report = findings_module.findings_by_path(PROJECT, root=fixture.repository)
    return {row["finding"]: row["disposition"] for row in report["rows"]}


def test_the_reader_reports_both_findings_open_before_a_disposition(
    fixture: Fixture,
) -> None:
    """The premise: the reader shows the two findings the record carries, both open."""
    assert _by_index(fixture) == {0: OPEN, 1: OPEN}


def test_a_fold_on_a_finding_is_reported_by_the_findings_reader(
    fixture: Fixture,
) -> None:
    """The command's fold reads back as ``folded:<node>`` and its neighbour stays open."""
    result = _dispose(
        fixture, "--finding", "1", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["finding_index"] == 1
    assert receipt["disposition"] == f"folded:{FOLD_NODE}"

    by_index = _by_index(fixture)
    assert by_index[1] == f"folded:{FOLD_NODE}"
    assert by_index[0] == OPEN


def test_an_exemption_records_its_reason_on_the_finding(fixture: Fixture) -> None:
    """The other kind in the closed set answers the finding and keeps the reason."""
    reason = "the finding is answered by the merged head reviewed beside it"
    result = _dispose(
        fixture, "--finding", "0", "--kind", "exempted", "--reason", reason
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["disposition"] == f"exempted:{reason}"

    by_index = _by_index(fixture)
    assert by_index[0] == f"exempted:{reason}"
    assert by_index[1] == OPEN


def test_a_finding_is_answered_where_the_dimension_writer_writes(
    fixture: Fixture,
) -> None:
    """The finding disposition lands in the record the store's own reader selects.

    The record rewritten is the file the dimension writer selects, and the entry
    is read back from that file rather than a copy a later read does not consult;
    it is written under the finding key, never the dimension map.
    """
    result = _dispose(
        fixture, "--finding", "1", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["path"] == str(fixture.review_file)

    stored = json.loads(fixture.review_file.read_text(encoding="utf-8"))
    recorded = stored[review_module.FINDING_DISPOSITIONS_KEY]
    assert recorded["1"]["kind"] == "folded"
    assert recorded["1"]["node"] == FOLD_NODE
    assert review_module.DIMENSION_DISPOSITIONS_KEY not in stored


REFUSALS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "exemption-without-a-reason",
        ("--finding", "1", "--kind", "exempted"),
        "exempted disposition must record",
    ),
    (
        "fold-carrying-a-reason",
        (
            "--finding",
            "1",
            "--kind",
            "folded",
            "--node",
            FOLD_NODE,
            "--reason",
            "carried over from an exemption that was never recorded",
        ),
        "folded disposition carries no reason",
    ),
    (
        "exemption-carrying-a-node",
        (
            "--finding",
            "1",
            "--kind",
            "exempted",
            "--reason",
            "answered by the merged head",
            "--node",
            FOLD_NODE,
        ),
        "exempted disposition names no node",
    ),
    (
        "unknown-kind",
        ("--finding", "1", "--kind", "noted", "--reason", "shorthand"),
        "unknown disposition",
    ),
    (
        "index-naming-no-finding",
        ("--finding", "7", "--kind", "folded", "--node", FOLD_NODE),
        "names none of them",
    ),
)


@pytest.mark.parametrize(
    "arguments, refusal",
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_a_refused_disposition_writes_nothing(
    fixture: Fixture, arguments: tuple[str, ...], refusal: str
) -> None:
    """A refusal exits non-zero, names itself, and leaves the record unchanged."""
    before = fixture.review_file.read_bytes()

    result = _dispose(fixture, *arguments)

    assert result.exit_code != 0, result.output
    assert refusal in result.output
    assert fixture.review_file.read_bytes() == before


def test_naming_both_a_dimension_and_a_finding_writes_nothing(
    fixture: Fixture,
) -> None:
    """The two spaces are mutually exclusive; naming both answers neither."""
    before = fixture.review_file.read_bytes()

    result = _dispose(
        fixture,
        "--dimension",
        "durability",
        "--finding",
        "1",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )

    assert result.exit_code != 0, result.output
    assert "--dimension and --finding" in result.output
    assert fixture.review_file.read_bytes() == before


def test_naming_neither_a_dimension_nor_a_finding_writes_nothing(
    fixture: Fixture,
) -> None:
    """One of the two spaces is required; naming neither is refused."""
    before = fixture.review_file.read_bytes()

    result = _dispose(fixture, "--kind", "folded", "--node", FOLD_NODE)

    assert result.exit_code != 0, result.output
    assert "--dimension and --finding" in result.output
    assert fixture.review_file.read_bytes() == before
