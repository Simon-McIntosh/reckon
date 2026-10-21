"""A single-run read reads that run and nothing else.

``runs_view`` grew a ``run_id`` selector. Identity is known before the read, so
the selector narrows the read rather than filtering its result: the live arm
reads one pointer instead of walking every pointer in the fleet, and the ledger
arm keeps one row before any compact row is built. Both arms would otherwise
materialise the whole fleet before an in-memory filter threw most of it away —
which is what made a one-directory read take tens of seconds on this host.

The file count is measured by patching ``io``'s ``open``, which is the reader
``pathlib.Path.read_text`` actually calls. A positive control first shows the
instrument sees a known-large count, so a small count is not an absence.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from reckon import crew, file_memo, ledger
from reckon.crew.query import runs_view

LEDGER_ROWS = 2000
TARGET = "r-target-run"

#: Run documents the synthetic ledger carries, so a non-indexed read has files
#: of its own to open rather than only the aggregate. Well above the handful a
#: one-run read is allowed, and small enough to write in a fixture.
CONTROL_DOCUMENTS = 150

#: A one-run read resolves its session from the row it already holds and decodes
#: no run document of its own, so it opens a host-independent handful of files
#: however many rows the ledger carries. Measured 2 through the source read and
#: 1 through the index on the synthetic ledger below.
MAX_OPEN_FILES = 4


def _repository(root: Path, project: str) -> Path:
    repository = root / f"{project}-repository"
    (repository / "docs" / "state" / project).mkdir(parents=True)
    return repository


def _write_rows(
    repository: Path, project: str, count: int, *, documents: int = 0
) -> None:
    """Write a ledger envelope directly, so the fixture cost does not scale with rows.

    ``documents`` also gives the first that many rows their own run file, built
    from the same row the aggregate holds so the two serialisations agree and
    the read accepts the pair as one history.
    """
    rows = [
        {
            "run_id": TARGET if index == count // 2 else f"r-{index:06d}",
            "plan": "run-query",
            "node": {"id": f"node-{index}", "plan": "run-query"},
            "commits": [],
        }
        for index in range(count)
    ]
    path = repository / "docs" / "state" / project / "crew.json"
    path.write_text(
        json.dumps(
            {
                "updated": "2026-09-25T00:00:00",
                "project": project,
                "doc": "crew",
                "data": {"members": [], "runs": rows, "holds": [], "_version": 1},
            }
        ),
        encoding="utf-8",
    )
    if documents:
        (path.parent / "runs").mkdir(parents=True, exist_ok=True)
    for row in rows[:documents]:
        ledger.write_run_file(project, row, repository, fsync=False)


@contextmanager
def _count_opens() -> Iterator[dict[str, int]]:
    """Count ``io.open`` calls for the duration of the block."""
    real = io.open
    tally = {"n": 0}

    def counting(path: Any, *args: Any, **kwargs: Any) -> Any:
        tally["n"] += 1
        return real(path, *args, **kwargs)

    io.open = counting
    try:
        yield tally
    finally:
        io.open = real


def _ledger_repository(tmp_path: Path, *, documents: int = 0) -> tuple[Path, str]:
    project = "alpha"
    repository = _repository(tmp_path, project)
    _write_rows(repository, project, LEDGER_ROWS, documents=documents)
    return repository, project


def test_one_run_read_returns_exactly_that_row(tmp_path: Path) -> None:
    repository, project = _ledger_repository(tmp_path)
    result = runs_view(project, checkout_path=str(repository), run_id=TARGET)
    assert result["count"] == 1
    assert [row["run_id"] for row in result["rows"]] == [TARGET]


def test_one_run_read_opens_a_fixed_small_number_of_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, project = _ledger_repository(tmp_path, documents=CONTROL_DOCUMENTS)
    # Warm any lazy import so the count is the read's own, not import-time I/O.
    runs_view(project, checkout_path=str(repository), run_id=TARGET)

    with monkeypatch.context() as patch:
        # SQLite reads bypass io.open, so the positive control uses source files.
        patch.setattr(ledger, "load", partial(ledger.load, use_index=False))
        # The count is of files this read opens, so drop any parse an earlier
        # read in this process left memoised for these paths.
        file_memo.clear()
        with _count_opens() as full:
            runs_view(project, checkout_path=str(repository))
        # Positive control: a non-indexed read opens the aggregate and then one
        # file per run document, so the instrument sees a count fixed by that
        # population — measured 151 here — and the small counts below are
        # readings the instrument made rather than opens it failed to see.
        assert full["n"] > CONTROL_DOCUMENTS, full

        with _count_opens() as one:
            result = runs_view(project, checkout_path=str(repository), run_id=TARGET)
        assert result["count"] == 1
        assert one["n"] <= MAX_OPEN_FILES, one

    with _count_opens() as indexed_one:
        result = runs_view(project, checkout_path=str(repository), run_id=TARGET)
    assert result["count"] == 1
    assert indexed_one["n"] <= MAX_OPEN_FILES, indexed_one


def test_one_run_read_selects_one_of_two_live_pointers() -> None:
    for run_id, node in (("r-aaa", "node-a"), ("r-bbb", "node-b")):
        crew._write_json(
            crew.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": "alpha",
                "repo": "/nonexistent",
                "node": {"id": node, "plan": "run-query"},
                "phase": "working",
                "process_alive": False,
            },
        )
    result = runs_view("alpha", source="live", run_id="r-bbb")
    assert result["count"] == 1
    assert result["rows"][0]["run_id"] == "r-bbb"
    assert result["rows"][0]["node"] == "node-b"


def test_unknown_run_id_returns_no_rows() -> None:
    result = runs_view("alpha", source="live", run_id="r-absent")
    assert result["count"] == 0
    assert result["rows"] == []
