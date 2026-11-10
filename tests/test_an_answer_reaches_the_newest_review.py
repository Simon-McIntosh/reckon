"""An answer reaches the newest review that raised the finding.

A plan accumulates several content reviews, and rounds reuse the same finding
ids (``wiring-1`` says the same thing in each). An answer naming only the
finding therefore has to land on the review a reader would see — the newest one
that raised it — and a caller who names an older review explicitly must reach
that one instead, through the CLI or through the crew tool. Where two covering
reviews cannot be ordered at all, the answer is refused rather than guessed.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew import plan_review
from reckon.mcp_crew_actions import crew_action
from tests.test_review_plan_command import project as project  # noqa: PLC0414

FINDING = "wiring-1"
OLDER = "r-20261105T210526990727"
NEWER = "r-20261106T062807421758"
OLDER_AT = "2026-11-05T21:05:26.990727+00:00"
NEWER_AT = "2026-11-06T06:28:07.421758+00:00"


def _review(
    path: Path,
    *,
    run_id: str,
    timestamp: str | None,
    fingerprint: bool = True,
    digests: dict[str, str] | None = None,
) -> dict:
    """Store one content review of the fixture plan raising ``wiring-1``."""
    record: dict = {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 3,
        "reviewed_blob_sha": "a" * 40,
        "review_run_id": run_id,
        "rubric": "content",
        "findings": [{"id": FINDING, "type": "wiring", "text": "Wire the section."}],
        "responses": {},
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    if fingerprint:
        record["plan_fingerprint"] = plan_review.plan_fingerprint(path)
    if digests is not None:
        record["section_digests"] = digests
    plan_review.store_plan_review(record)
    if timestamp is None:
        # The store stamps every record it writes, so a record the store cannot
        # order is one a hand wrote: drop the stamp the store just added.
        stored = _stored(run_id)
        stripped = {key: value for key, value in stored.items() if key != "timestamp"}
        Path(stored["review_path"]).write_text(
            json.dumps(stripped, indent=2, sort_keys=True), encoding="utf-8"
        )
    return record


def _stored(run_id: str) -> dict:
    for record in plan_review.list_plan_reviews("sample", "fixture"):
        if plan_review._record_run_id(record) == run_id:
            return record
    raise AssertionError(f"no stored review names run {run_id!r}")


def _answered(run_id: str) -> bool:
    return bool((_stored(run_id).get("responses") or {}).get(FINDING))


def _answer(*arguments: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            FINDING,
            "--acted",
            "--note",
            "The wiring now names its owner.",
            *arguments,
        ],
    )
    return result.exit_code, result.output


def _two_reviews(path: Path, **options) -> None:
    _review(path, run_id=OLDER, timestamp=OLDER_AT, **options)
    _review(path, run_id=NEWER, timestamp=NEWER_AT, **options)


def test_read_plan_review_returns_the_newer_of_two_equal_versions(
    project,
) -> None:
    """Two whole-plan reviews at one version: the newest by timestamp is served."""
    _, _, path = project
    _two_reviews(path)

    record = plan_review.read_plan_review("sample", "fixture", plan=path)

    assert record is not None
    assert plan_review._record_run_id(record) == NEWER


def test_an_answer_without_a_review_run_lands_on_the_newer_review(project) -> None:
    """The older review's answer is left exactly as it was."""
    _, _, path = project
    _two_reviews(path)

    code, output = _answer()

    assert code == 0, output
    assert _answered(NEWER), "the answer did not reach the newer review"
    assert not _answered(OLDER), "the answer overwrote the older review"


def test_an_answer_naming_the_older_review_is_written_there(project) -> None:
    """``--review-run`` reaches a review that is not the newest."""
    _, _, path = project
    _two_reviews(path)

    code, output = _answer("--review-run", OLDER)

    assert code == 0, output
    assert _answered(OLDER), "the named review was not answered"
    assert not _answered(NEWER), "the answer also landed on the newer review"


def test_the_mcp_answer_action_writes_to_the_named_review(project) -> None:
    """The crew tool carries the same review run as the CLI."""
    _, _, path = project
    _two_reviews(path)

    answer = crew_action(
        "answer",
        project="sample",
        plan="fixture",
        finding=FINDING,
        acted=True,
        note="The wiring now names its owner.",
        session="coordinator",
        review_run=OLDER,
    )

    assert answer.get("ok") is not False, answer
    assert _answered(OLDER), "the tool's answer did not reach the named review"
    assert not _answered(NEWER), "the tool's answer also landed on the newer review"


def test_read_plan_review_returns_the_newer_when_fingerprints_do_not_match(
    project,
) -> None:
    """The per-section branch returns both covering reviews; the newest is served."""
    _, _, path = project
    digests, _ = plan_review._unit_digest_forms(path)
    _two_reviews(path, fingerprint=False, digests=digests)

    contributing = plan_review.review_coverage("sample", "fixture", plan=path)[0]
    assert sorted(plan_review._record_run_id(record) for record in contributing) == [
        OLDER,
        NEWER,
    ], "the per-section branch did not return both reviews as covering"

    record = plan_review.read_plan_review("sample", "fixture", plan=path)

    assert record is not None
    assert plan_review._record_run_id(record) == NEWER


def test_an_unorderable_covering_pair_is_refused_naming_both(project) -> None:
    """A record with no timestamp names no position, so the answer is refused."""
    _, _, path = project
    _review(path, run_id=OLDER, timestamp=OLDER_AT)
    _review(path, run_id=NEWER, timestamp=None)

    code, output = _answer()

    assert code != 0, f"the ambiguous answer was accepted:\n{output}"
    assert OLDER in output, f"the refusal did not name the older review:\n{output}"
    assert NEWER in output, f"the refusal did not name the newer review:\n{output}"
    assert not _answered(OLDER) and not _answered(NEWER), (
        "a refused answer still wrote a response"
    )
