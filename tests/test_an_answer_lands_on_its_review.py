"""An answer lands on the stored review whose finding it names.

Reviews at successive plan commits can carry equal or non-monotonic version
integers, so the version integer cannot say which review read the newest plan.
The reader a caller is served, and the record an answer is written to, follow
the plan's commit order instead: ancestry of each record's recorded plan
commit, falling back to the review's timestamp when a commit is unreachable.

The fixture is a synthesised checkout under ``RECKON_HOME``. Its plan content
is unchanged across three commits — only the server-managed version scalar
moves — so all three reviews cover the plan and the selector is what decides
between them. The timestamps deliberately run against the commit order, so a
selector that quietly fell back to them would serve the oldest review.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import plan_review
from tests.test_review_plan_command import project as project  # noqa: PLC0414

PROJECT = "sample"
PLAN_SLUG = "fixture"
PLAN_PATH = "docs/plans/fixture.html"

OLDEST_FINDING = "alpha"
MIDDLE_FINDING = "beta"
NEWEST_FINDING = "gamma"
ORPHAN_FINDING = "delta"
OLDEST_RUN = "run-oldest"
MIDDLE_RUN = "run-middle"
NEWEST_RUN = "run-newest"
ORPHAN_RUN = "run-orphan"
DEFAULT_PROSE = "Extend the existing mechanism."


def _plan(version: int, prose: str = DEFAULT_PROSE) -> str:
    """The fixture plan, whose only change between commits is its version.

    ``prose`` is the authored body the fingerprint digests, so a review stored
    against one prose stops covering the plan once that prose changes.
    """
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-version" content="{version}">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        f"<p>{prose}</p></body></html>"
    )


def _commit_version(
    repo: Path, path: Path, version: int, message: str, prose: str = DEFAULT_PROSE
) -> tuple:
    """Write the plan at ``version``, commit it, and return its commit and blob."""
    path.write_text(_plan(version, prose), encoding="utf-8")
    for args in (["add", PLAN_PATH], ["commit", "-q", "-m", message]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout.decode()
    blob = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"HEAD:{PLAN_PATH}"],
        check=True,
        capture_output=True,
    ).stdout.decode()
    return commit.strip(), blob.strip()


def _store(
    path: Path,
    *,
    version: int,
    commit: str,
    blob: str,
    run_id: str,
    finding: str,
    timestamp: str,
    fingerprint: str | None = None,
) -> None:
    """Store one content review of the plan as it stood at ``commit``."""
    plan_review.store_plan_review(
        {
            "project": PROJECT,
            "plan_slug": PLAN_SLUG,
            "plan_version": version,
            "reviewed_blob_sha": blob,
            "plan_commit": commit,
            "plan_fingerprint": (
                fingerprint
                if fingerprint is not None
                else plan_review.plan_fingerprint(path)
            ),
            "rubric": "content",
            "review_run_id": run_id,
            "findings": [
                {
                    "id": finding,
                    "type": "correctness",
                    "text": f"The finding only {run_id} raises.",
                }
            ],
            "responses": {},
            "status": "ready",
            "timestamp": timestamp,
        }
    )


@pytest.fixture
def three_reviews(project):
    """Three reviews at successive commits with non-increasing versions."""
    _, repo, path = project
    oldest = _commit_version(repo, path, 7, "chore: pause the plan at version seven")
    middle = _commit_version(repo, path, 3, "chore: restate the plan version as three")
    newest = _commit_version(repo, path, 5, "chore: advance the plan version to five")
    # The timestamps run against the commit order: the oldest review is stamped
    # last, so a selector that fell back to timestamps would serve it.
    _store(
        path,
        version=7,
        commit=oldest[0],
        blob=oldest[1],
        run_id=OLDEST_RUN,
        finding=OLDEST_FINDING,
        timestamp="2026-11-07T03:00:00+00:00",
    )
    _store(
        path,
        version=3,
        commit=middle[0],
        blob=middle[1],
        run_id=MIDDLE_RUN,
        finding=MIDDLE_FINDING,
        timestamp="2026-11-07T02:00:00+00:00",
    )
    _store(
        path,
        version=5,
        commit=newest[0],
        blob=newest[1],
        run_id=NEWEST_RUN,
        finding=NEWEST_FINDING,
        timestamp="2026-11-07T01:00:00+00:00",
    )
    return repo, path, (oldest, middle, newest)


def _stored_by_run() -> dict[str, dict]:
    return {
        str(record.get("review_run_id")): record
        for record in plan_review.list_plan_reviews(PROJECT, PLAN_SLUG)
    }


def _answer(*options: str):
    return CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            PROJECT,
            "--plan",
            PLAN_SLUG,
            *options,
        ],
    )


def test_the_view_serves_the_review_at_the_newest_commit(three_reviews) -> None:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "view",
            "plan-review",
            "--project",
            PROJECT,
            "--plan",
            PLAN_SLUG,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    record = payload["record"]
    assert record is not None
    assert record["review_run_id"] == NEWEST_RUN, (
        "the newest commit's review is served, not the highest version integer"
    )
    assert plan_review.finding_ids(record) == [NEWEST_FINDING]


def test_answer_writes_to_the_review_its_finding_names(three_reviews) -> None:
    result = _answer(
        "--answer",
        NEWEST_FINDING,
        "--acted",
        "--note",
        "The newest review's finding is answered by this edit.",
    )
    assert result.exit_code == 0, result.output

    stored = _stored_by_run()
    assert NEWEST_FINDING in stored[NEWEST_RUN]["responses"], (
        "the answer lands on the review at the newest commit"
    )
    assert not stored[OLDEST_RUN]["responses"], (
        "no answer lands on the review at the older commit"
    )
    assert not stored[MIDDLE_RUN]["responses"]


def test_review_run_naming_the_middle_review_answers_its_finding(
    three_reviews,
) -> None:
    # The middle review raises a finding no other review raises, so only the
    # named review can answer it.
    result = _answer(
        "--answer",
        MIDDLE_FINDING,
        "--review-run",
        MIDDLE_RUN,
        "--acted",
        "--note",
        "The middle review's finding is answered by this edit.",
    )
    assert result.exit_code == 0, result.output

    stored = _stored_by_run()
    assert MIDDLE_FINDING in stored[MIDDLE_RUN]["responses"], (
        "the named middle review takes the answer"
    )
    assert not stored[OLDEST_RUN]["responses"]
    assert not stored[NEWEST_RUN]["responses"]


def test_review_run_naming_a_review_that_did_not_raise_the_finding_is_refused(
    three_reviews,
) -> None:
    """A name the store cannot honour is refused, and nothing is answered.

    ``--review-run`` selects among the reviews that raise the finding, so a
    name matching none of them is a mistake in the command rather than a
    reason to write the answer onto whichever review is newest.
    """
    result = _answer(
        "--answer",
        MIDDLE_FINDING,
        "--review-run",
        NEWEST_RUN,
        "--acted",
        "--note",
        "The named review does not raise this finding.",
    )
    assert result.exit_code == 1, result.output
    error = json.loads(result.stdout)
    assert error["error"] == "crew_error"
    assert "--review-run" in error["detail"]
    assert MIDDLE_RUN in error["detail"], (
        "the refusal names the reviews that do raise it"
    )

    stored = _stored_by_run()
    assert not any(record["responses"] for record in stored.values()), (
        "a refused answer is written to no review"
    )


def test_review_run_reaches_a_review_that_no_longer_covers_the_plan(project) -> None:
    """A named review answers even when no stored review covers the plan.

    The plan's prose changes after the review read it, so nothing covers the
    plan any more and the command has no newest review to fall back on. The
    finding is still owed by the review that raised it, and naming that review
    is how a caller reaches it.
    """
    _, repo, path = project
    commit, blob = _commit_version(repo, path, 2, "chore: record the plan as reviewed")
    reviewed = plan_review.plan_fingerprint(path)
    _commit_version(
        repo,
        path,
        4,
        "chore: settle the plan after that review",
        prose="Replace the existing mechanism.",
    )
    _store(
        path,
        version=2,
        commit=commit,
        blob=blob,
        run_id=ORPHAN_RUN,
        finding=ORPHAN_FINDING,
        timestamp="2026-11-07T04:00:00+00:00",
        fingerprint=reviewed,
    )
    assert plan_review.read_plan_review(PROJECT, PLAN_SLUG, plan=path) is None, (
        "the premise: the stored review does not cover the plan as it stands, "
        "so the unnamed answer has nothing to land on"
    )

    result = _answer(
        "--answer",
        ORPHAN_FINDING,
        "--review-run",
        ORPHAN_RUN,
        "--acted",
        "--note",
        "The named review's finding is answered by this edit.",
    )
    assert result.exit_code == 0, result.output

    stored = _stored_by_run()
    assert ORPHAN_FINDING in stored[ORPHAN_RUN]["responses"], (
        "the named review takes the answer though it no longer covers the plan"
    )
