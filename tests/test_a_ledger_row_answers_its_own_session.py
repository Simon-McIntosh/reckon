"""A ledger row answers its own session from the row the view already holds.

The runs view compacts every committed row of a project before it filters,
sorts and applies ``limit``. A promoted run has no live pointer and no live
stream, so its session used to be answered by loading the whole ledger once per
row, looking for the row the caller had already read. One view therefore made a
number of ledger loads equal to the size of the ledger. A row that carries its
own committed record answers from that record, so one view makes at most one
load per project and every row keeps the answer base produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import query, resumption

PROJECT = "proj"
ROWS_WITH_SESSION = 51

#: The absence a row reports when none of the three sources has an id. It names
#: all three, because *not yet observed* and *genuinely absent* are different
#: facts and a bare null expressed neither.
STATED_ABSENCE_FRAGMENTS = ("pointer", "stream", "ledger")


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    return root


def _populate(repository: Path) -> list[str]:
    """Promote rows into a synthetic ledger; the last records no session."""
    run_ids: list[str] = []
    for index in range(ROWS_WITH_SESSION + 1):
        run_id = f"r-20261021T10{index:04d}000000-node-{index:02d}"
        ledger.append_run(
            PROJECT,
            ledger.build_record(
                run_id=run_id,
                plan="plan-a",
                section="§2",
                node=f"node-{index:02d}",
                gate="passed",
                member_id="member-b",
                session_id=(
                    f"session-{index:03d}" if index < ROWS_WITH_SESSION else None
                ),
                base_sha="def456",
                commits=["fedcba"],
            ),
            root=repository,
        )
        run_ids.append(run_id)
    return run_ids


def _base_answers(run_ids: list[str], repository: Path) -> dict[str, tuple]:
    """What the resolver answers for a ledger row with no row supplied.

    This is the base path for a promoted run: its pointer is gone and it has no
    live stream, so the ledger source answers. Asking the resolver itself keeps
    the expectation tied to the ledger a run actually recorded rather than to a
    literal a future change would leave behind.
    """
    answers: dict[str, tuple] = {}
    for run_id in run_ids:
        answer = resumption.resolve_session(run_id, project=PROJECT, root=repository)
        answers[run_id] = (answer["session_id"], answer["source"], answer["resolved"])
    return answers


def test_a_ledger_row_answers_its_own_session(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_ids = _populate(repository)
    expected = _base_answers(run_ids, repository)

    loads = {"count": 0}
    real_load = ledger.load

    def counting_load(*args: object, **kwargs: object):
        loads["count"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(ledger, "load", counting_load)
    answer = query.runs_view(PROJECT, checkout_path=str(repository), source="ledger")

    assert loads["count"] <= 1, (
        f"one runs view made {loads['count']} ledger.load calls over "
        f"{len(run_ids)} promoted rows"
    )
    rows = {str(row["run_id"]): row for row in answer["rows"]}
    assert set(rows) == set(run_ids)

    for run_id, (session_id, source, resolved) in expected.items():
        row = rows[run_id]
        assert (row["session_id"], row["session_id_source"]) == (
            session_id,
            source,
        ), f"{run_id} answered {row['session_id_source']!r} not {source!r}"
        assert row["resumable"] is False or resolved
        if not resolved:
            reason = str(row["resumable_reason"])
            assert all(fragment in reason for fragment in STATED_ABSENCE_FRAGMENTS), (
                f"{run_id} reports {reason!r}, which does not name all three sources"
            )
