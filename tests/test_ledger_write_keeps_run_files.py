"""A ledger write leaves each per-run file identical to its aggregate row.

A completed run can be recorded twice — once in the aggregate run list and once
in its own file under ``runs/`` — and :func:`ledger.load` refuses the whole
project when the two serialisations disagree. A write that edited an aggregate
row and left the file holding the previous revision therefore handed its own
caller a project its reader could not read: the writer produced state the
reader refuses. Measured on the shadow-dispatch fixtures, where each edits a run
record through ``ledger.write`` and every subsequent read died inside
``ledger.load`` with the differs-between refusal.

The writer is the defect, so the writer is what this file pins: a write keeps
every per-run file equal to its aggregate row, and a disagreement introduced by
editing a file directly, outside the writer is still refused. Every fixture
here is built in a temporary repository; the closing assertion in the first
case proves the real configuration home was never written to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "proj"
RUN_ID = "r-20260929T000000000000-node-a"


@pytest.fixture()
def repo(tmp_path):
    """A throwaway checkout carrying this project's state directory."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    return root


def _seed(repo: Path) -> dict:
    """Record one run in the aggregate and in its own file, as a promotion does.

    The aggregate row is written through the writer, then the per-run file is
    placed directly so the fixture starts in the dual-placement state the
    refusal is about — both copies present and already in agreement.
    """
    row = {
        "run_id": RUN_ID,
        "gate": "passed",
        "outcome": "completed with a recorded budget",
        "time_budget": "12m",
    }
    ledger.write(
        PROJECT,
        {"members": [], "runs": [dict(row)], "holds": []},
        0,
        repo,
    )
    target = ledger.run_path(PROJECT, RUN_ID, repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ledger.serialize_run(row), encoding="utf-8")
    return row


def _file_text(repo: Path) -> str:
    return ledger.run_path(PROJECT, RUN_ID, repo).read_text(encoding="utf-8")


def test_an_edited_row_is_written_to_its_per_run_file(repo) -> None:
    """Editing a row through the writer keeps the file equal to the row.

    Read the run, drop a field, write it back — the read that follows must not
    be refused, and the per-run file must carry the same edit rather than the
    field the row no longer holds.
    """
    _seed(repo)

    data, version = ledger.load(PROJECT, repo)
    edited = data["runs"][0]
    edited.pop("time_budget")
    ledger.write(PROJECT, data, version, repo)

    reread, _ = ledger.load(PROJECT, repo)
    assert "time_budget" not in reread["runs"][0]
    assert "time_budget" not in json.loads(_file_text(repo))
    assert _file_text(repo) == ledger.serialize_run(reread["runs"][0])

    # The write stayed inside the temporary repository.
    path = ledger.ledger_path(PROJECT, repo)
    assert str(path).startswith(str(repo))
    assert not Path(os.environ["RECKON_HOME"]).is_relative_to(Path.home())


def test_a_file_edited_outside_the_writer_is_still_refused(repo) -> None:
    """The falsifier: a direct file edit is a disagreement the reader refuses.

    With the aggregate left untouched, a per-run file carrying different bytes
    is exactly the state :func:`ledger.load` must refuse, so keeping the writer
    honest must not make the reader tolerant of a hand-edited file.
    """
    row = _seed(repo)

    tampered = dict(row, outcome="completed by a hand edit")
    ledger.run_path(PROJECT, RUN_ID, repo).write_text(
        ledger.serialize_run(tampered), encoding="utf-8"
    )

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.load(PROJECT, repo)

    assert "differs between" in str(excinfo.value)
    assert RUN_ID in str(excinfo.value)
