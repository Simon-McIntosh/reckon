"""A blocking finding in a run's figure review opens the run's repair round.

A run that drew a figure carries two stored reviews at one head: its text review
and its figure review. The repair round is composed from the *run's* findings, so
a blocking finding the figure reviewer raised has to reach the round — otherwise
a figure defect waits for a coordinator while a text defect goes back to the node
that drew it. This drives the composer the reflex reads
(:func:`reckon.crew.repair.compose_repair_for_run`) and reads back the resume
advice its node produces, pinning the three cases the round must distinguish: a
figure finding that blocks opens the round and is named by the advice; a figure
finding that only follows on opens nothing; and a text blocking finding composes
the round it composed before, unchanged.

The figure review is read here through ``read_review`` with the figure rubric and
its severities through ``blocking_findings`` — the one severity owner — rather
than a second reader that could drift from it. Every store is built under the
test's own temporary tree and nothing outside it is read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery_repair_dispatch as repair_dispatch
from reckon.crew import repair
from reckon.crew import review as review_module

PROJECT = "sample"
RUN_ID = "run-under-review"
HEAD = "a" * 40
FIGURE = "docs/figures/sample/plot.svg"

FIGURE_FINDING = {
    "file": FIGURE,
    "line": "",
    "text": "the axis label is clipped at the canvas edge",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}
FIGURE_FOLLOW_ON = {
    "file": FIGURE,
    "line": "",
    "text": "the legend could state the units",
    "severity": "follow-on",
}
TEXT_FINDING = {
    "file": "reckon/crew/thing.py",
    "line": "12",
    "text": "the loop off-by-one the reviewer found",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose mounts and review store both live under the test tree."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    docs = tmp_path / "repo" / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">',
        encoding="utf-8",
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    return reviews


def _store_text_review(store: Path, findings: list[dict[str, str]]) -> None:
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "review_run_id": "text-review-run",
            "reviewed_base_sha": HEAD,
            "reviewed_head_sha": HEAD,
            "status": "parsed",
            "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
            "findings": findings,
        },
        base_dir=store,
    )


def _store_figure_review(store: Path, findings: list[dict[str, str]]) -> None:
    review_module.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "review_run_id": "figure-review-run",
            "reviewed_base_sha": HEAD,
            "reviewed_head_sha": HEAD,
            "rubric": review_module.FIGURE_REVIEW_RUBRIC,
            "status": "parsed",
            "rubric_items": dict.fromkeys(review_module.FIGURE_REVIEW_ITEMS, "pass"),
            "findings": findings,
        },
        base_dir=store,
    )


def _compose(store: Path) -> dict | None:
    return repair.compose_repair_for_run(
        PROJECT,
        RUN_ID,
        base_dir=str(store),
        reviewed_head_sha=HEAD,
    )


def _advice(composed: dict) -> str:
    """The resume advice the reflex would carry for a composed round."""
    return repair_dispatch._repair_resume_advice(
        composed, composed["write_paths"], composed["round_id"]
    )


def _read_figure(store: Path) -> dict:
    figure = review_module.read_review(
        PROJECT,
        RUN_ID,
        base_dir=store,
        reviewed_head_sha=HEAD,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )
    assert figure is not None
    return figure


def test_a_blocking_figure_finding_opens_the_round_and_names_the_figure(
    store: Path,
) -> None:
    """The positive case: the figure finding is the work the round answers."""
    _store_text_review(store, [])
    _store_figure_review(store, [FIGURE_FINDING])

    # The severity is read through the store's own owner, not a second reader.
    figure = _read_figure(store)
    blocking = repair.blocking_findings(figure)
    assert [finding["text"] for finding in blocking] == [FIGURE_FINDING["text"]]

    composed = _compose(store)
    assert composed is not None

    advice = _advice(composed)
    assert FIGURE in advice
    assert FIGURE_FINDING["text"] in advice
    assert FIGURE in composed["brief"]
    assert FIGURE_FINDING["text"] in composed["brief"]
    assert FIGURE in composed["write_paths"]


def test_a_follow_on_figure_finding_opens_no_round(store: Path) -> None:
    """Only a blocking figure finding is work; a follow-on is recorded."""
    _store_text_review(store, [])
    _store_figure_review(store, [FIGURE_FOLLOW_ON])

    figure = _read_figure(store)
    assert repair.blocking_findings(figure) == []
    assert _compose(store) is None


def test_a_text_blocking_finding_still_composes_the_same_round(
    store: Path,
) -> None:
    """A run with no figure review composes the round it composed before."""
    _store_text_review(store, [TEXT_FINDING])

    composed = _compose(store)
    assert composed is not None
    advice = _advice(composed)
    assert TEXT_FINDING["file"] in advice
    assert TEXT_FINDING["text"] in advice
    assert FIGURE not in advice


def test_text_and_figure_blocking_findings_compose_one_round(store: Path) -> None:
    """Both reviews at one head contribute to the single round a round is."""
    _store_text_review(store, [TEXT_FINDING])
    _store_figure_review(store, [FIGURE_FINDING])

    composed = _compose(store)
    assert composed is not None
    assert len(composed["findings"]) == 2
    advice = _advice(composed)
    assert TEXT_FINDING["file"] in advice
    assert FIGURE in advice
    assert FIGURE_FINDING["text"] in advice
