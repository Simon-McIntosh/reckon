"""The crew summary read carries a node's wall time and its width at dispatch.

Every test enters through :func:`reckon.crew.summary.run_rows` — the read a
caller reaches — rather than through the arithmetic underneath it, because the
defect this covers is in the read's answer rather than in the subtraction. The
repository is a throwaway directory written by the fixture, so no test reads or
writes the real crew state, and a reading here is a statement about the code.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import summary

PROJECT = "proj"


def _row(run_id: str, dispatched: str | None, completed: str | None, **extra):
    """One committed row, carrying the stamps the read derives the figures from.

    A stamp passed as ``None`` is omitted rather than written as null, so the
    missing-stamp case is a row that genuinely does not carry the field.
    """

    record = {"run_id": run_id, "plan": "a-plan", "node": run_id}
    if dispatched is not None:
        record["dispatched_at"] = dispatched
    if completed is not None:
        record["completed_at"] = completed
    record.update(extra)
    return record


# The fixture ranges the input domain rather than one shape: two runs dispatched
# at the same moment, a row whose stored field disagrees with its own stamps, a
# row missing its completion stamp, and a row missing its dispatch stamp.
ROWS = [
    _row(
        "early",
        "2026-09-20T10:00:00Z",
        "2026-09-20T10:30:00Z",
        wall_seconds=1800,
    ),
    _row(
        "disagreeing",
        "2026-09-20T10:00:00Z",
        "2026-09-20T10:02:00Z",
        wall_seconds=4242,
    ),
    _row(
        "no-completion",
        "2026-09-20T10:10:00Z",
        None,
        wall_seconds=7,
    ),
    _row(
        "alone",
        "2026-09-20T11:00:00Z",
        "2026-09-20T11:01:00Z",
    ),
    _row(
        "no-dispatch",
        None,
        "2026-09-20T10:20:00Z",
        wall_seconds=999,
    ),
]


def _read(tmp_path: Path) -> dict[str, dict]:
    """Stage a repository carrying one ledger, then enter the summary read."""

    repo = tmp_path / "repo"
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "crew.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "data": {
                    "_version": 3,
                    "members": [],
                    "runs": ROWS,
                    "holds": [],
                },
            }
        )
        + "\n"
    )
    return {row["run_id"]: row for row in summary.run_rows(PROJECT, root=repo)}


def test_ledger_fixture_is_untouched_by_the_read(tmp_path):
    """The read leaves the repository it read from unchanged."""

    rows = _read(tmp_path)
    assert set(rows) == {"early", "disagreeing", "no-completion", "alone", "no-dispatch"}
    before = (tmp_path / "docs").exists()
    assert before is False  # the real docs directory is never the fixture's
    assert (tmp_path / "repo" / "docs" / "state" / PROJECT / "crew.json").is_file()


def test_every_row_carries_wall_time_derived_from_its_own_stamps(tmp_path):
    """Every run row carries the span between its dispatch and completion."""

    rows = _read(tmp_path)
    assert rows["early"]["wall_seconds"] == 1800
    assert rows["disagreeing"]["wall_seconds"] == 120
    assert rows["alone"]["wall_seconds"] == 60
    for row in rows.values():
        assert "wall_seconds" in row


def test_a_disagreeing_stored_field_never_reaches_the_answer(tmp_path):
    """The answer carries the stamp-derived figure, never the stored one."""

    rows = _read(tmp_path)
    stored = {row["run_id"]: row.get("wall_seconds") for row in ROWS}
    # The fixture's stored ``wall_seconds`` disagrees with its own stamps on
    # three rows, so none of those values may appear under the answer's name.
    assert stored["disagreeing"] == 4242
    assert stored["no-completion"] == 7
    assert stored["no-dispatch"] == 999
    assert rows["disagreeing"]["wall_seconds"] == 120
    assert rows["no-completion"]["wall_seconds"] is None
    assert rows["no-dispatch"]["wall_seconds"] is None
    for run_id in ("disagreeing", "no-completion", "no-dispatch"):
        assert rows[run_id]["wall_seconds"] != stored[run_id]
    # The one row whose stored field agrees reports that value because it is the
    # derived one, not because the stored one was passed through: the read never
    # consults the stored field at all.
    assert rows["early"]["wall_seconds"] == stored["early"] == 1800


def test_every_row_carries_a_width_at_start(tmp_path):
    """Width at start counts the runs in flight at the row's dispatch stamp."""

    rows = _read(tmp_path)
    # Two runs share the 10:00 dispatch moment, so each sees the other.
    assert rows["early"]["width_at_start"] == 2
    assert rows["disagreeing"]["width_at_start"] == 2
    # Dispatched at 10:10, inside the one span still open then.
    assert rows["no-completion"]["width_at_start"] == 1
    # Dispatched alone at 11:00.
    assert rows["alone"]["width_at_start"] == 1
    for row in rows.values():
        assert "width_at_start" in row


def test_a_missing_stamp_reports_absence_rather_than_zero(tmp_path):
    """A figure the stamps cannot support is absent, and never zero."""

    rows = _read(tmp_path)
    assert rows["no-completion"]["wall_seconds"] is None
    assert rows["no-completion"]["wall_seconds"] != 0
    assert rows["no-dispatch"]["wall_seconds"] is None
    assert rows["no-dispatch"]["wall_seconds"] != 0
    assert rows["no-dispatch"]["width_at_start"] is None
    assert rows["no-dispatch"]["width_at_start"] != 0