"""Per-dimension review scores are readable from committed state.

A stored review's five dimensions are written onto the promoted run's committed
ledger row, and nothing read them back: the reader existed with no caller, so a
score was recorded and never consulted. These tests hold the crew ``scores``
view to the three distinctions the committed block is built to preserve — a
reviewed row answers with its own dimensions under each of the three filters, a
row with no stored review is left out rather than returned as a row of zeros,
and a stored review that never parsed answers with its status and no scores.

The fixture is a temporary repository holding one ledger, and the config home is
pointed at an empty temporary directory: a view that answered from anywhere but
the checked-out ledger would come back empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import ledger, mcp
from reckon.crew import review as review_module

PROJECT = "sample-project"
DIMENSIONS = review_module.REVIEW_DIMENSIONS

# Each dimension carries a different value, so a dimension returned against the
# wrong name is visible rather than hidden by the total.
REVIEWED_SCORES = {
    "goal_fidelity": 18,
    "evidence": 15,
    "scope_discipline": 17,
    "durability": 5,
    "fit": 16,
}


def _parsed_block(scores: dict[str, int]) -> dict:
    """The ledger block a promotion stores for a parsed review."""

    return review_module.ledger_block(
        {"status": "parsed", "scores": scores, "total": sum(scores.values())}
    )


@pytest.fixture()
def ledger_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage a throwaway repository carrying one committed ledger."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    # build_record and ledger_block are the production path by which a
    # promotion derives a row's review block, so the fixture holds the shape
    # the committed ledger actually carries.
    rows = [
        ledger.build_record(
            run_id="run-reviewed",
            plan="plan-a",
            node="node-alpha",
            gate="passed",
            review=_parsed_block(REVIEWED_SCORES),
        ),
        # Same plan and node as the reviewed row: it matches every filter that
        # row matches, so its absence is the reader's doing and not the
        # filter's.
        ledger.build_record(
            run_id="run-plain",
            plan="plan-a",
            node="node-alpha",
            gate="passed",
        ),
        ledger.build_record(
            run_id="run-zero",
            plan="plan-a",
            node="node-beta",
            gate="passed",
            review=_parsed_block(dict.fromkeys(DIMENSIONS, 0)),
        ),
        ledger.build_record(
            run_id="run-unparsed",
            plan="plan-a",
            node="node-beta",
            gate="passed",
            # The block an unparseable review reduces to: a status other than
            # "parsed", empty scores, and no total.
            review=review_module.ledger_block(
                {
                    "status": "unparsed",
                    "scores": {},
                    "absent": list(DIMENSIONS),
                    "total": None,
                }
            ),
        ),
        ledger.build_record(
            run_id="run-other-plan",
            plan="plan-b",
            node="node-alpha",
            gate="passed",
            review=_parsed_block(dict.fromkeys(DIMENSIONS, 10)),
        ),
    ]
    for row in rows:
        ledger.append_run(PROJECT, row, root=root, allow_create=True)
    return root


def _scores(repo: Path, **filters: str) -> dict[str, dict]:
    """Read the committed rows through the crew surface a caller would use."""

    payload = mcp._crew(PROJECT, view="scores", checkout_path=str(repo), **filters)
    assert payload["ok"] is True, payload
    return {row["run_id"]: row for row in payload["scores"]}


def test_the_reviewed_rows_dimensions_come_back_filtered_by_plan(
    ledger_repo: Path,
) -> None:
    rows = _scores(ledger_repo, plan="plan-a")

    assert set(rows) == {"run-reviewed", "run-zero", "run-unparsed"}
    reviewed = rows["run-reviewed"]
    assert reviewed["plan"] == "plan-a"
    assert reviewed["node"] == "node-alpha"
    assert reviewed["status"] == "parsed"
    assert set(reviewed["scores"]) == set(DIMENSIONS)
    assert reviewed["scores"] == REVIEWED_SCORES
    assert reviewed["absent"] == []
    assert reviewed["total"] == sum(REVIEWED_SCORES.values())


def test_the_reviewed_row_comes_back_filtered_by_node(ledger_repo: Path) -> None:
    rows = _scores(ledger_repo, node="node-alpha")

    assert set(rows) == {"run-reviewed", "run-other-plan"}
    assert rows["run-reviewed"]["scores"] == REVIEWED_SCORES


def test_the_reviewed_row_comes_back_filtered_by_run_id(ledger_repo: Path) -> None:
    rows = _scores(ledger_repo, run_id="run-reviewed")

    assert set(rows) == {"run-reviewed"}
    assert rows["run-reviewed"]["scores"] == REVIEWED_SCORES


def test_an_unreviewed_row_is_left_out_rather_than_returned_as_zeros(
    ledger_repo: Path,
) -> None:
    # Under a filter that matches it exactly, an unreviewed row contributes no
    # row at all: a review that was never stored is not a score of zero, and a
    # caller must not be able to read it as one.
    assert _scores(ledger_repo, run_id="run-plain") == {}
    # The same row under the node it shares with the reviewed row.
    assert "run-plain" not in _scores(ledger_repo, node="node-alpha")


def test_a_review_that_scored_zero_is_not_the_absent_review(ledger_repo: Path) -> None:
    rows = _scores(ledger_repo, run_id="run-zero")

    assert set(rows) == {"run-zero"}
    assert rows["run-zero"]["status"] == "parsed"
    assert rows["run-zero"]["scores"] == dict.fromkeys(DIMENSIONS, 0)
    assert rows["run-zero"]["total"] == 0


def test_an_unparsed_review_returns_its_status_with_no_invented_scores(
    ledger_repo: Path,
) -> None:
    rows = _scores(ledger_repo, run_id="run-unparsed")

    assert set(rows) == {"run-unparsed"}
    unparsed = rows["run-unparsed"]
    assert unparsed["status"] == "unparsed"
    assert unparsed["scores"] == {}
    assert unparsed["total"] is None
    assert set(unparsed["absent"]) == set(DIMENSIONS)
