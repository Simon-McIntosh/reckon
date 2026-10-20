"""A figure review and the run's text review are separate subjects.

A run review's supersession is derived from the record and its subject. The
subject of a run review is its reviewed run *and* the rubric it was filed
under: a figure review and a text review of one run at one head are two
subjects, as a plan's design and content reviews are, so a figure review filed
after the text review leaves the text review current and neither reads
superseded. Within one rubric the order still holds — a later text review
supersedes an earlier text review, and a later figure review an earlier figure
review — and a run carrying only text reviews reads exactly as it did before.

Every record here is built and stored through the store's own writer,
:func:`reckon.crew.review.store_review`, rather than written by hand, so the
``rubric`` field the store writes and the field the derivation reads cannot
drift apart. The stores and the checkout live in a temporary directory, and the
config home is redirected there, so no test reads or writes outside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import review_lifecycle as module

PROJECT = "figure-text-fixture"
REVIEWED_RUN = "r-20261020T120000000000-reviewed-run"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _store_record(
    store: Path,
    *,
    head: str,
    dispatched: str,
    review_run: str,
    rubric: str | None = None,
    findings: list | None = None,
) -> Path:
    """Build one run review through the store's writer and return its path.

    ``dispatched_at`` is set explicitly so the supersede order is a fact of the
    fixture rather than of when the test happened to run. ``rubric`` is omitted
    for a text review, which is how the store spells "no rubric".
    """
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": review_run,
        "status": "parsed",
        "dispatched_at": dispatched,
        "reviewed_base_sha": head,
        "reviewed_head_sha": head,
        "findings": findings or [],
    }
    if rubric is not None:
        record["rubric"] = rubric
    return review_module.store_review(record, base_dir=store)


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised config home and project checkout under the temp tree."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def test_a_figure_review_does_not_supersede_the_run_text_review(
    checkout: Path, tmp_path: Path
) -> None:
    # The text review is filed first, then a figure review at the same head.
    # Under the rubric-inclusive subject both stay current; under the old rule
    # the later figure review would supersede the text review.
    store = tmp_path / "store"
    text_path = _store_record(
        store,
        head=HEAD_A,
        dispatched="2026-10-20T01:00:00+00:00",
        review_run="text-review-1",
    )
    figure_path = _store_record(
        store,
        head=HEAD_A,
        dispatched="2026-10-20T02:00:00+00:00",
        review_run="figure-review-1",
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )
    assert text_path != figure_path

    states = module.review_lifecycles(PROJECT, base_dir=store, root=checkout)

    assert states[str(text_path)] == module.ANSWERED
    assert states[str(figure_path)] == module.ANSWERED


def test_a_later_text_review_still_supersedes_the_earlier_text_review(
    checkout: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    earlier = _store_record(
        store,
        head=HEAD_A,
        dispatched="2026-10-20T01:00:00+00:00",
        review_run="text-review-1",
    )
    later = _store_record(
        store,
        head=HEAD_B,
        dispatched="2026-10-20T02:00:00+00:00",
        review_run="text-review-2",
    )

    states = module.review_lifecycles(PROJECT, base_dir=store, root=checkout)

    assert states[str(earlier)] == module.SUPERSEDED
    assert states[str(later)] == module.ANSWERED


def test_a_later_figure_review_still_supersedes_the_earlier_figure_review(
    checkout: Path, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    earlier = _store_record(
        store,
        head=HEAD_A,
        dispatched="2026-10-20T01:00:00+00:00",
        review_run="figure-review-1",
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )
    later = _store_record(
        store,
        head=HEAD_B,
        dispatched="2026-10-20T02:00:00+00:00",
        review_run="figure-review-2",
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )

    states = module.review_lifecycles(PROJECT, base_dir=store, root=checkout)

    assert states[str(earlier)] == module.SUPERSEDED
    assert states[str(later)] == module.ANSWERED


def test_a_text_only_run_reads_as_it_did_before(checkout: Path, tmp_path: Path) -> None:
    # A run with only text reviews: a blocking finding reads open, a follow-on
    # only review reads answered, and neither is superseded by the other's
    # existence under a different rubric because there is no other rubric here.
    store = tmp_path / "store"
    answered_path = _store_record(
        store,
        head=HEAD_A,
        dispatched="2026-10-20T01:00:00+00:00",
        review_run="text-review-1",
        findings=[{"id": "f1", "severity": "follow-on", "text": "a follow-up"}],
    )
    open_path = _store_record(
        store,
        head=HEAD_B,
        dispatched="2026-10-20T02:00:00+00:00",
        review_run="text-review-2",
        findings=[{"id": "f2", "severity": "blocking", "text": "needs repair"}],
    )

    states = module.review_lifecycles(PROJECT, base_dir=store, root=checkout)

    assert states[str(answered_path)] == module.SUPERSEDED
    assert states[str(open_path)] == module.OPEN


def test_the_run_subject_carries_the_rubric_family() -> None:
    # The derivation's own predicate: a figure rubric is its own subject, and a
    # record under no rubric or another rubric reads as a text review.
    text = {"reviewed_run_id": REVIEWED_RUN}
    figure = {"reviewed_run_id": REVIEWED_RUN, "rubric": "figure"}
    other = {"reviewed_run_id": REVIEWED_RUN, "rubric": "claims"}
    assert module._run_subject(text) == (REVIEWED_RUN, False)
    assert module._run_subject(figure) == (REVIEWED_RUN, True)
    assert module._run_subject(other) == (REVIEWED_RUN, False)
