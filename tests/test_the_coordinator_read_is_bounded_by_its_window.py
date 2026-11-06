"""A coordinator read skips documents written before its own window.

The resolver keeps only runs dispatched inside a staleness window, so a per-run
document last written before that window, less a margin, cannot hold a row the
resolver keeps. Reading it anyway costs a read, a parse and a copy per document,
and a project's ledger holds thousands. This pins the bound on the document's own
modification time, never on the instant its run id was minted: a run id is
stamped when its request is queued while the document is written when the run is
promoted, so a long-queued run has an old id and a recent document.

The fixture is hermetic: ``RECKON_HOME`` is a temp tree, the checkout holding the
plans and the ledger is a temp directory, and every document's modification time
is set explicitly so no case depends on when the suite runs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import plan_coordinator
from reckon.crew.node import parse_duration

PROJECT = "reckon"

#: The instant every case resolves at, so the window, the margin and the
#: fixture's own stamps are all fixed arithmetic.
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
WINDOW = "168h"

#: The lower bound the resolver derives: its horizon, less a day of margin. The
#: fixture's stamps sit far from it on both sides, so the exact margin does not
#: decide any case here; the ancient document is 200 days back and the two read
#: documents are within two days of now.
BOUND = NOW - timedelta(seconds=parse_duration(WINDOW)) - timedelta(days=1)

#: Each fixture document: run id (also its file stem), plan, dispatching
#: session, dispatch instant, and the instant its document was last written.
#: The write instant is the file's own mtime, set with os.utime below.
ANCIENT = (
    "r-20260101T000000000000-ancient-work",
    "plan-ancient",
    "session-ancient",
    "2026-01-01T00:00:00Z",
    NOW - timedelta(days=200),
)
INSIDE = (
    "r-20261105T090000000000-inside-work",
    "plan-inside",
    "session-inside",
    "2026-11-05T09:00:00Z",
    NOW - timedelta(hours=27),
)
#: Queued long before the window and dispatched inside it. Its run id stamp is
#: before the bound; its document was written after the dispatch, inside it.
LONG_QUEUED = (
    "r-20260920T080000000000-long-queued",
    "plan-queued",
    "session-queued",
    "2026-11-04T10:00:00Z",
    NOW - timedelta(hours=50),
)

ROWS = (ANCIENT, INSIDE, LONG_QUEUED)


def _write_plan(docs: Path, slug: str) -> None:
    plans = docs / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{slug}.html").write_text(
        "<!doctype html>\n<html><head>"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="plan-owner" content="Simon McIntosh">\n'
        f"</head><body><h1>{slug}</h1></body></html>\n",
        encoding="utf-8",
    )


def _write_rows(root: Path) -> None:
    """Promote each fixture row into its own document, then stamp its mtime."""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    ledger.run_path(PROJECT, ROWS[0][0], root=root).parent.mkdir(
        parents=True, exist_ok=True
    )
    for run_id, plan, session, dispatched_at, written_at in ROWS:
        _write_plan(docs, plan)
        ledger.write_run_file(
            PROJECT,
            ledger.build_record(
                run_id=run_id,
                plan=plan,
                node=f"{plan}-node",
                session=session,
                dispatched_at=dispatched_at,
                completed_at=dispatched_at,
                gate="passed",
            ),
            root=root,
        )
        path = ledger.run_path(PROJECT, run_id, root=root)
        stamp = written_at.timestamp()
        os.utime(path, (stamp, stamp))


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_rows(root)
    return root


def _resolve(root: Path) -> dict:
    return plan_coordinator.resolve_plan_coordinators(
        PROJECT, root=root, now=NOW, window=WINDOW, pointers=[]
    )


def _record_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every document the code reads, through the reader it uses."""
    read: list[str] = []
    real = ledger._read_run

    def recording(source: Path) -> dict:
        read.append(source.stem)
        return real(source)

    monkeypatch.setattr(ledger, "_read_run", recording)
    return read


def test_no_document_written_before_the_bound_is_read(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = _record_reads(monkeypatch)
    _resolve(project_root)
    # The positive control: the reader is reached for the documents inside the
    # bound, so an empty read list cannot make the assertion below pass.
    assert INSIDE[0] in read
    assert LONG_QUEUED[0] in read
    assert ANCIENT[0] not in read


def test_the_ancient_document_is_readable_when_no_bound_is_passed(
    project_root: Path,
) -> None:
    """The same fixture read without the bound reaches the ancient document.

    Without this arm the case above could pass because that file is unreadable
    or absent rather than because the bound skipped it.
    """
    read: list[str] = []
    real = ledger._read_run

    def recording(source: Path) -> dict:
        read.append(source.stem)
        return real(source)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ledger, "_read_run", recording)
        rows = ledger.run_rows_from_documents(PROJECT, root=project_root)
    assert ANCIENT[0] in read
    assert {row["run_id"] for row in rows} == {row[0] for row in ROWS}


def test_the_long_queued_run_keeps_its_coordinator(project_root: Path) -> None:
    plans = _resolve(project_root)["plans"]
    assert plans[LONG_QUEUED[1]]["coordinator"] == LONG_QUEUED[2]
    assert plans[INSIDE[1]]["coordinator"] == INSIDE[2]
    # Nothing dispatches plan-ancient inside the window, so the plan is still
    # named and still unowned, rather than dropped with the history behind it.
    assert plans[ANCIENT[1]]["coordinator"] is None


def test_the_bound_is_the_document_time_not_the_run_id(project_root: Path) -> None:
    """The long-queued run's id stamp sits before the bound while its document
    does not, so the two bounds would answer differently for it.
    """
    id_stamp = datetime.strptime(LONG_QUEUED[0][2:17], "%Y%m%dT%H%M%S").replace(
        tzinfo=UTC
    )
    assert id_stamp < BOUND
    assert LONG_QUEUED[4] >= BOUND
    ancient_stamp = datetime.strptime(ANCIENT[0][2:17], "%Y%m%dT%H%M%S").replace(
        tzinfo=UTC
    )
    assert ancient_stamp < BOUND
    assert ANCIENT[4] < BOUND


def test_the_bounded_answer_equals_the_unbounded_answer(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound skips history only, so every plan resolves as it did before.

    The unbounded read is the base revision's behaviour reproduced by dropping
    the bound from the call, and the comparison is against what that read
    actually returns for this fixture rather than a written-down answer.
    """
    bounded = _resolve(project_root)["plans"]
    real = ledger.run_rows_from_documents

    def unbounded(project: str, root=None, **_kwargs):
        return real(project, root=root)

    monkeypatch.setattr(ledger, "run_rows_from_documents", unbounded)
    base = _resolve(project_root)["plans"]
    assert bounded == base
    assert base[LONG_QUEUED[1]]["coordinator"] == LONG_QUEUED[2]
