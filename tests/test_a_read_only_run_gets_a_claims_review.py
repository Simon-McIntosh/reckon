"""A read-only run's review checks the claims its report makes, not a diff.

An investigation, or a brief-carried run that committed nothing past its base,
leaves no commit and runs no gate suite, so the landed-node rubric's six
dimensions and its added-failure count measure nothing the run did. The review
the reflex composes for such a run carries the claims rubric instead: the
reviewer re-runs the commands the report cites and records whether each claim
reproduces. These cases compose that review through
:func:`recovery._review_dispatch_fields`, drive a fixed claims emission through
the parser and the promotion gate, and hold a landed implement run's review to
the rubric it had before.

The negative control removes the read-only role test, so the investigate run
falls through to the landed-node done-when: the first case then fails on the
missing claims rubric, which is the red this node's log records.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import promotion_checks, recovery, runs
from reckon.crew import review as review_module

_CLAIMS_EMISSION = (
    "CLAIM 1: reproduced: the option-usage scan reports the same three options\n"
    "CLAIM 2: differs: the report says 42 modules; the re-run produced 40\n"
    "CLAIM 3: not-runnable: the script names no input file to read\n"
    "CLAIM_SUMMARY: differs: the summary says 12 modules; the table lists 14\n"
)


@pytest.fixture
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated crew home, so a run directory resolves under ``tmp_path``."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A committed tree that a run's worktree can point at."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _write_report(run_id: str) -> Path:
    """A simulated investigation report under the run's own directory."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "report.md"
    report.write_text(
        "# Findings\n\n"
        "The option-usage scan reports 42 modules and 12 unused options.\n",
        encoding="utf-8",
    )
    return report


def _investigate_record(repository: Path, run_id: str) -> dict:
    """A live pointer for a read-only investigate run realised at a plan section.

    The plan section is what makes the role test load-bearing: a brief-carried
    run with no commit is caught by the brief clause whether or not the role is
    read, so the investigate case names a plan the way the read-only runs the
    reflex reviews do.
    """
    return {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repository),
        "worktree": str(repository),
        "role": "investigate",
        "node": {
            "id": "health-scan",
            "plan": "fixture",
            "section": "s2",
            "brief_path": str(repository / "brief.md"),
        },
    }


def _implement_record(repository: Path) -> dict:
    """A live pointer for a landed implement run carrying a plan section."""
    return {
        "run_id": "r-implement-run",
        "project": "sample",
        "repo": str(repository),
        "worktree": str(repository),
        "role": "implement",
        "node": {
            "id": "landed-node",
            "plan": "fixture",
            "section": "s2",
            "brief_path": str(repository / "brief.md"),
        },
    }


def test_an_investigate_run_is_composed_with_the_claims_rubric(
    crew_home: Path, repository: Path
) -> None:
    run_id = "r-investigate-run"
    _write_report(run_id)

    fields = recovery._review_dispatch_fields(_investigate_record(repository, run_id))

    done_when = fields["done_when"].lower()
    assert "claims rubric" in done_when
    assert "added_failure_count" not in fields["done_when"]
    assert "claim" in done_when and "claim_summary" in done_when


def test_a_brief_carried_run_with_no_commit_is_read_only_too(
    crew_home: Path, repository: Path
) -> None:
    run_id = "r-brief-run"
    _write_report(run_id)
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": _head(repository),
        "node": {
            "id": "brief-worker",
            "plan": "",
            "section": "",
            "brief_path": str(repository / "brief.md"),
        },
    }

    fields = recovery._review_dispatch_fields(record)

    assert "claims rubric" in fields["done_when"].lower()
    assert "added_failure_count" not in fields["done_when"]


def test_a_landed_implement_run_keeps_the_landed_node_rubric(
    crew_home: Path, repository: Path
) -> None:
    fields = recovery._review_dispatch_fields(_implement_record(repository))

    assert "claims rubric" not in fields["done_when"].lower()
    assert "added_failure_count" in fields["done_when"]
    assert f"{len(review_module.REVIEW_DIMENSIONS)} dimensions" in fields["done_when"]


def test_three_claim_lines_parse_to_a_complete_record_promotion_accepts() -> None:
    record = review_module.parse_review(_CLAIMS_EMISSION)

    assert record["status"] == "parsed"
    assert record["rubric"] == review_module.CLAIMS_RUBRIC
    assert len(record["claims"]) == review_module.CLAIMS_REQUIRED
    assert recovery._review_is_complete(record) is True

    # Promotion reads the same record through its review gate: a parsed record
    # satisfies it, so the waiver returns without refusing.
    pointer = {
        "run_id": "r-investigate-run",
        "project": "sample",
        "role": "investigate",
    }
    accepted = promotion_checks._require_review_waiver(
        "r-investigate-run",
        pointer,
        verdict="passed",
        classification="scoring",
        review=record,
        review_action="",
        waiver_reason="",
        review_tier="full",
        promoted_head="",
        stale_head="",
        manifest_commits=(),
    )
    assert accepted is None


def test_a_claims_record_carries_no_suite_delta_and_a_short_one_is_incomplete() -> None:
    record = review_module.parse_review(_CLAIMS_EMISSION)
    assert "added_failure_count" not in record
    assert "added_failure_ids" not in record

    short = review_module.parse_review(
        "CLAIM 1: reproduced: same figure\nCLAIM_SUMMARY: agrees: matches\n"
    )
    assert short["status"] == "parsed"
    assert recovery._review_is_complete(short) is False


def _head(repository: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
