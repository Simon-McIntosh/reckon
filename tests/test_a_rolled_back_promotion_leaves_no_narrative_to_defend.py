"""A rolled-back promotion leaves no narrative to defend.

A promotion writes the plan's landing comment before it appends the run's
promotion row. When the row write fails, ``ledger.py`` rolls the row back and
the comment survives, committed, with nothing behind it: no row records that
narrative, so no promotion ever reported it. A retry with a corrected outcome
must therefore treat the comment as left over from the failed attempt and
record the narrative it is promoting now.

A run whose row was written is a different case. There the row is the record
and the narrative it pinned is defended: a later call with other wording is
reported beside the receipt and the plan's comment is left alone.

Both cases run against a temporary crew home and a temporary plan repository.
"""
# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

from pathlib import Path

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion
from reckon.crew.runs import pointer_path
from tests.test_a_run_can_always_be_reconciled import (  # noqa: F401 - fixture
    PLAN,
    PROJECT,
    _pointer,
    repository,
)

STALE = "review scored 91, 1 finding(s)"
CORRECTED = "review scored 94, 2 finding(s) after the corrected pass"


def _promote(repository: Path, run_id: str, *, outcome: str) -> dict:
    return crew.complete(run_id, gate="not-run", outcome=outcome, root=repository)


def _landing_comment_bodies(repository: Path) -> list[str]:
    state = _plan_html.read_state_file(repository / "docs" / "plans" / f"{PLAN}.html")
    return [
        item.get("body", "")
        for items in (state.get("comments") or {}).values()
        for item in items
    ]


def _write_comment_with_no_row(repository: Path, run_id: str, narrative: str) -> None:
    """Leave the state a rolled-back promotion leaves: a comment, no row."""
    promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="s1",
        run_id=run_id,
        narrative=narrative,
        author="reckon-build",
        when="2026-10-01T06:05:00Z",
        root=repository,
        worker_tree=repository,
    )
    assert ledger.runs(PROJECT, root=repository) == []


# ── Case (a): the comment outlived a row that was rolled back ───────────────


def test_a_comment_with_no_row_is_replaced_by_the_narrative_promoted_now(
    repository: Path,
) -> None:
    """No row means nothing recorded that text, so the retry supersedes it."""
    run_id = "r-rolled-back-promotion"
    _write_comment_with_no_row(repository, run_id, STALE)
    _pointer(repository, run_id)

    receipt = _promote(repository, run_id, outcome=CORRECTED)

    rows = ledger.runs(PROJECT, root=repository)
    assert len(rows) == 1
    assert not pointer_path(run_id).exists()
    bodies = _landing_comment_bodies(repository)
    assert len(bodies) == 1, "the comment keeps its id instead of gaining a second"
    assert CORRECTED in bodies[0]
    assert STALE not in bodies[0]
    comment = receipt["plan_comment"]
    assert comment["recorded"] is True
    assert comment["superseded_narrative"] == STALE


# ── Case (b): the row exists, so its narrative is the record ────────────────


def test_a_written_row_keeps_its_narrative_and_reports_the_other(
    repository: Path,
) -> None:
    """A committed row pins the narrative, so a later wording is reported."""
    run_id = "r-recorded-promotion"
    _pointer(repository, run_id)
    _promote(repository, run_id, outcome=STALE)
    _pointer(repository, run_id)

    receipt = _promote(repository, run_id, outcome=CORRECTED)

    assert receipt["already_promoted"] is True
    assert len(ledger.runs(PROJECT, root=repository)) == 1
    comment = receipt["plan_comment"]
    assert comment["reason"] == "different_narrative"
    assert comment["kept_narrative"] == STALE
    assert comment["requested_narrative"] == CORRECTED
    bodies = _landing_comment_bodies(repository)
    assert any(STALE in body for body in bodies)
    assert not any(CORRECTED in body for body in bodies)
