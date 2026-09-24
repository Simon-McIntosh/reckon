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
from pathlib import Path
from typing import Any

from reckon import crew
from reckon.crew.query import runs_view

LEDGER_ROWS = 2000
TARGET = "r-target-run"

#: The run-id path reads one ledger file and resolves one row's session from it,
#: so it opens a host-independent handful of files. Measured 2 on the synthetic
#: ledger below; the full walk over the same ledger opens 2001.
MAX_OPEN_FILES = 4


def _repository(root: Path, project: str) -> Path:
    repository = root / f"{project}-repository"
    (repository / "docs" / "state" / project).mkdir(parents=True)
    return repository


def _write_rows(repository: Path, project: str, count: int) -> None:
    """Write a ledger envelope directly, so the fixture cost does not scale with rows."""
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


def _ledger_repository(tmp_path: Path) -> tuple[Path, str]:
    project = "alpha"
    repository = _repository(tmp_path, project)
    _write_rows(repository, project, LEDGER_ROWS)
    return repository, project


def test_one_run_read_returns_exactly_that_row(tmp_path: Path) -> None:
    repository, project = _ledger_repository(tmp_path)
    result = runs_view(project, checkout_path=str(repository), run_id=TARGET)
    assert result["count"] == 1
    assert [row["run_id"] for row in result["rows"]] == [TARGET]


def test_one_run_read_opens_a_fixed_small_number_of_files(tmp_path: Path) -> None:
    repository, project = _ledger_repository(tmp_path)
    # Warm any lazy import so the count is the read's own, not import-time I/O.
    runs_view(project, checkout_path=str(repository), run_id=TARGET)

    with _count_opens() as full:
        runs_view(project, checkout_path=str(repository))
    # Positive control: the instrument sees the walk's per-row ledger reopen.
    assert full["n"] > 100, full

    with _count_opens() as one:
        result = runs_view(project, checkout_path=str(repository), run_id=TARGET)
    assert result["count"] == 1
    assert one["n"] <= MAX_OPEN_FILES, one


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
