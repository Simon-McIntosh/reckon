"""A blocking figure finding holds a promotion until it is answered.

A figure review protects nothing unless promotion reads it. The other review
guards key on a stored record's status and the revision it read, so a figure
review carrying a blocking finding satisfies them and the defect it names lands
unrepaired. This module pins the guard that closes that: ``crew complete`` reads
the run's newest figure review and refuses while a blocking finding is open,
naming the figure the finding concerns.

The severity is read through the one owner that decides it
(:func:`reckon.crew.repair.blocking_findings`), so an answered finding no longer
refuses, and a run with no figure review is promoted exactly as it is today —
text reviews keep their present behaviour. A figure review that cannot be read
refuses as well, because the store answers "no figure review" the same way for a
record it could not parse, and the record that could not be read is the one that
might hold the finding.

Every record is built through the store's own writer,
:func:`reckon.crew.review.store_review`, so the field the store files a record
under and the field this reader selects on cannot drift apart. The stores and
the configuration home live under a temporary directory, so nothing here reads
or writes outside it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import promotion, promotion_checks, review_dispositions
from reckon.crew import review as review_module
from reckon.crew.node import CrewError

PROJECT = "blocking-figure-fixture"
PLAN = "blocking-figure-plan"
RUN = "r-20261020T120000000000-blocking-figure-run"
TEXT_REVIEW_RUN = "r-20261020T120500000000-text-review"
FIGURE_REVIEW_RUN = "r-20261020T121000000000-figure-review"
HEAD = "a" * 40

FIGURE_PATH = "docs/figures/blocking-figure-plan/node/labels.svg"
FIGURE_CLAIM = "the y-axis label is clipped at the canvas edge"


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=tree, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _record(
    *,
    review_run: str,
    rubric: str | None,
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "review_run_id": review_run,
        "status": "parsed",
        "reviewed_base_sha": HEAD,
        "reviewed_head_sha": HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": findings,
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }
    if rubric is not None:
        record["rubric"] = rubric
    return record


def _blocking_figure_finding() -> dict[str, str]:
    return {
        "file": FIGURE_PATH,
        "line": "1",
        "text": FIGURE_CLAIM,
        "severity": "blocking",
    }


@dataclass(frozen=True)
class Fixture:
    """The temporary world one assertion reads its stores from."""

    repository: Path
    record: dict[str, Any]
    figure_path: Path


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Fixture:
    """A text review with no blocking finding and a blocking figure review.

    ``isolated_reckon_home`` redirects the crew configuration home into the
    temporary tree, so the review store these records are written to — and read
    back from — sits under it rather than in the operator's own store.
    """
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed blocking-figure fixture"),
    ):
        _git(repository, *arguments)

    review_module.store_review(
        _record(review_run=TEXT_REVIEW_RUN, rubric=None, findings=[])
    )
    figure_path = review_module.store_review(
        _record(
            review_run=FIGURE_REVIEW_RUN,
            rubric=review_module.FIGURE_REVIEW_RUBRIC,
            findings=[_blocking_figure_finding()],
        )
    )
    record = {
        "run_id": RUN,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "role": "implement",
        "node": {"id": RUN, "plan": "", "write_paths": ["docs"]},
    }
    return Fixture(repository=repository, record=record, figure_path=figure_path)


def _dispose_the_figure_finding(fixture: Fixture, *, reason: str) -> None:
    """Record the figure finding's disposition on the figure review record.

    The entry is built through the disposition module's own entry builder and
    written back through the store's writer, so the answering lands under the
    key and in the shape every reader of a disposition consults. The figure
    review is rewritten in place rather than at a second path, which is what
    keeps the record the promotion reads carrying the answer.
    """
    stored = review_module.stored_record(
        PROJECT,
        RUN,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )[1]
    assert stored is not None
    answer = review_dispositions._build_disposition_entry("exempted", None, reason)
    review_module.store_review(
        {**stored, review_module.FINDING_DISPOSITIONS_KEY: {"0": answer}}
    )


def test_an_undisposed_blocking_figure_finding_refuses_the_promotion(
    fixture: Fixture,
) -> None:
    """The premise: the refusal names the figure and the finding's own claim."""
    with pytest.raises(CrewError) as error:
        promotion_checks._require_no_undisposed_blocking_figure_finding(
            RUN, fixture.record
        )

    refusal = str(error.value)
    assert FIGURE_PATH in refusal
    assert FIGURE_CLAIM in refusal
    assert RUN in refusal


def test_a_disposed_blocking_figure_finding_promotes(fixture: Fixture) -> None:
    """The same run with its finding answered no longer refuses."""
    _dispose_the_figure_finding(fixture, reason="answered by the redrawn figure")

    promotion_checks._require_no_undisposed_blocking_figure_finding(RUN, fixture.record)


def test_a_blocking_text_finding_with_no_figure_review_promotes(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """A text review's blocking finding is not this guard's business."""
    review_module.store_review(
        _record(
            review_run=TEXT_REVIEW_RUN,
            rubric=None,
            findings=[
                {
                    "file": "reckon/crew/promotion_checks.py",
                    "line": "10",
                    "text": "a text defect nothing about a figure",
                    "severity": "blocking",
                }
            ],
        )
    )

    promotion_checks._require_no_undisposed_blocking_figure_finding(
        RUN, {"project": PROJECT}
    )


def test_a_figure_review_that_cannot_be_read_refuses_the_promotion(
    fixture: Fixture,
) -> None:
    """A figure record that exists but does not parse holds the promotion.

    The store answers ``None`` for a record it cannot read just as it does for a
    run carrying none, so a guard acting on that answer alone lands a run whose
    figure review — the one place a blocking finding could sit — was never read.
    The refusal names the file and the error the store met, which is what an
    operator needs to repair or remove it.
    """
    fixture.figure_path.write_text("{ not a review record", encoding="utf-8")

    with pytest.raises(CrewError) as error:
        promotion_checks._require_no_undisposed_blocking_figure_finding(
            RUN, fixture.record
        )

    refusal = str(error.value)
    assert str(fixture.figure_path) in refusal
    assert "cannot be read" in refusal


def test_a_text_review_that_cannot_be_read_does_not_refuse(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """A figure-marked name is what makes an unreadable record this guard's subject.

    A run with no figure review is promoted whatever state its text review is
    in, so a text record met mid-write must not hold the promotion: the guard
    reads the figure rubric alone, and it recognises a figure record by the name
    the store files it under rather than by parsing it — which is the reading an
    unreadable file does not allow.
    """
    text_path = review_module.review_path(
        PROJECT, RUN, reviewed_head_sha=HEAD, rubric=None
    )
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("{ not a review record", encoding="utf-8")

    promotion_checks._require_no_undisposed_blocking_figure_finding(
        RUN, {"project": PROJECT}
    )


def test_the_landing_preconditions_carry_the_figure_refusal(
    fixture: Fixture, tmp_path: Path
) -> None:
    """The landing path crew complete runs reaches the guard and refuses with it."""
    with pytest.raises(CrewError) as error:
        promotion._landing_preconditions(
            RUN,
            fixture.record,
            checkout=fixture.repository,
            ledger_root=tmp_path / "ledger",
            commits=(),
            gate="passed",
            failure_classification="",
            no_impl_change="",
            plan_link="",
            unplanned_reason="",
            boundary_waiver="",
            negative_control_waiver=None,
            accepted_paths=None,
            gate_check=None,
            require_gate_check=False,
        )

    assert FIGURE_PATH in str(error.value)
