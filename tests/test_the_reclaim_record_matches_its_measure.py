"""The reclaim record's figures are asserted, not only written down.

An evidence fragment is a static document: nothing executes it, so a figure
edited by hand drifts from the pass it describes and every later reader
inherits the drift. These cases pin what the record's fence required — the
four store sizes, the two populations and the removal counts — to the
arithmetic among them, so an edit either stays internally consistent or fails
here. They read only files in the repository and touch no real crew store, so
the assertions describe the document rather than the machine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENT = (
    REPO_ROOT
    / "docs/evidence/fragments/the-crew-home-has-a-bound"
    / "the-existing-backlog-is-reclaimed.html"
)
PLAN = REPO_ROOT / "docs/plans/the-crew-home-has-a-bound.html"
NODE_ID = "the-existing-backlog-is-reclaimed"
VERIFIED_SECTION = "s5"
MEASURES = (
    "~/.cache/reckon",
    "~/.config/reckon/crew/runs",
    "reports",
    "locks",
    "run directories",
    "run-store entries",
    "worktrees registered",
)


@pytest.fixture(scope="module")
def record() -> str:
    return FRAGMENT.read_text(encoding="utf-8")


def _plain(cell: str) -> str:
    return re.sub(r"<[^>]+>", "", cell).strip()


def _rows(record: str) -> list[list[str]]:
    """The table's body rows as plain-text cells; header rows carry no cells."""
    rows = []
    for group in re.findall(r"<tr>(.*?)</tr>", record, re.DOTALL):
        cells = [
            _plain(cell) for cell in re.findall(r"<td>(.*?)</td>", group, re.DOTALL)
        ]
        if cells:
            rows.append(cells)
    return rows


def _row(record: str, measure: str) -> list[str]:
    for cells in _rows(record):
        if measure in cells[0]:
            return cells
    raise AssertionError(f"the record carries no row for {measure!r}")


def _first_int(cell: str) -> int:
    match = re.search(r"\d[\d,]*", cell)
    assert match is not None, f"{cell!r} carries no number"
    return int(match.group().replace(",", ""))


def _signed_delta(cell: str) -> int:
    magnitude = _first_int(cell)
    minus_sign = "\N{MINUS SIGN}"
    return (
        -magnitude if minus_sign in cell or cell.strip().startswith("-") else magnitude
    )


def test_the_record_names_its_plan_and_the_section_it_verifies(record: str) -> None:
    """A record whose section anchor does not resolve is unreadable, not merely stale."""
    assert '<meta name="docs-project" content="reckon">' in record
    verified = re.search(r'<meta name="plan-verifies" content="([^"]+)">', record)
    assert verified is not None
    anchor = verified.group(1).rsplit("#", 1)[-1]
    assert anchor == VERIFIED_SECTION
    assert f'id="{anchor}"' in PLAN.read_text(encoding="utf-8")


def test_every_element_id_carries_the_node_prefix_and_is_unique(record: str) -> None:
    ids = re.findall(r'\sid="([^"]+)"', record)
    assert ids
    assert [i for i in ids if not i.startswith(NODE_ID)] == []
    assert [i for i in set(ids) if ids.count(i) > 1] == []


def test_the_four_store_sizes_and_the_two_populations_are_present(record: str) -> None:
    for measure in MEASURES:
        assert len(_row(record, measure)) == 4, measure


def test_every_delta_is_the_difference_of_its_own_row(record: str) -> None:
    """A delta that disagrees with its two readings is the drift this file prevents."""
    for measure in MEASURES:
        cells = _row(record, measure)
        before, after = _first_int(cells[1]), _first_int(cells[2])
        assert _signed_delta(cells[3]) == after - before, measure


def test_the_run_store_delta_is_stated_in_kib_not_restated_as_gigabytes(
    record: str,
) -> None:
    cells = _row(record, "~/.config/reckon/crew/runs")
    difference = _first_int(cells[1]) - _first_int(cells[2])
    assert f"{difference:,} KiB" in record


def test_the_removal_counts_the_record_claims_are_the_ones_it_states(
    record: str,
) -> None:
    assert "4,636 stale run locks" in record
    assert "3 report trees" in record
    assert "5 worktrees" in record
    assert "no</strong> run directory" in record
    assert "CHECK VERDICT: NO OVERLAP" in record


def test_the_record_separates_its_own_removal_from_the_store_wide_drop(
    record: str,
) -> None:
    """The two are different facts; a reader who conflates them credits this pass."""
    assert "2,410 run directories" in record
    assert "not</strong> this pass's" in record
    assert "show no run directory removed" in record
