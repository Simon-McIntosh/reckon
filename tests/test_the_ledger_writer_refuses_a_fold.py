"""The ledger writer refuses to fold per-run rows into the aggregate.

A promoted run lives in its own file under ``runs/`` beside the aggregate, and
:func:`ledger.load` returns the union of both so a reader sees one history. A
caller that hands that union back to :func:`ledger.write` copies every per-run
row into the tracked aggregate beside its own file. Measured 2026-10-17: a hold
check grew ``docs/state/reckon/crew.json`` from 62 MB to 81 MB that way, and a
release rewrite took it from 81 MB to 350 MB, past the host's 100 MB file
limit and unpushable.

Two shapes are guarded here. ``ledger.write`` refuses a write that would fold
rows the aggregate does not hold while their per-run files exist, naming how
many it would have folded. ``record_hold_checks`` reads the aggregate's own
rows, so the hold transition it records cannot carry them in.

Every case drives a throwaway project under ``tmp_path`` whose aggregate holds
ten rows and whose ``runs/`` holds fifty files, so the two sources disagree by
a known count before the write under test runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "proj"
AGGREGATE_ROWS = 10
PER_RUN_FILES = 50
OPENED_AT = "2026-10-17T12:00:00Z"


def _row(run_id: str) -> dict:
    return {"run_id": run_id, "gate": "passed"}


@pytest.fixture()
def project(tmp_path) -> Path:
    """A throwaway checkout whose aggregate holds ten rows and fifty files.

    The fifty rows are placed directly under ``runs/`` so the fixture starts in
    the split state the refusal is about: each of them is one record held by its
    own file, and none of them is a row of the aggregate.
    """
    root = tmp_path / "repo"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    ledger.write(
        PROJECT,
        {
            "members": [],
            "runs": [_row(f"a-{index:03d}") for index in range(AGGREGATE_ROWS)],
            "holds": [],
        },
        0,
        root,
    )
    for index in range(PER_RUN_FILES):
        run_id = f"f-{index:03d}"
        target = ledger.run_path(PROJECT, run_id, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ledger.serialize_run(_row(run_id)), encoding="utf-8")
    return root


def _aggregate(root: Path) -> dict:
    """The aggregate's own contents, never the union :func:`ledger.load` returns."""
    data, _version = ledger._load_aggregate(PROJECT, root)
    return data


def _aggregate_text(root: Path) -> str:
    return ledger.ledger_path(PROJECT, root).read_text(encoding="utf-8")


def _run_files(root: Path) -> dict[str, bytes]:
    directory = ledger.ledger_path(PROJECT, root).parent / "runs"
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.json"))}


def test_a_write_of_the_loaded_union_is_refused_naming_the_folded_rows(
    project: Path,
) -> None:
    """The falsifier: the union is a read shape, and a write of it is refused.

    The union is asserted to carry both sources before the refusal is expected,
    so a refusal that fired because the instrument saw nothing is not readable
    as this one.
    """
    union, version = ledger.load(PROJECT, project)
    assert len(union["runs"]) == AGGREGATE_ROWS + PER_RUN_FILES
    assert len(_aggregate(project)["runs"]) == AGGREGATE_ROWS

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, union, version, project)

    message = str(excinfo.value)
    assert "fold 50 run row(s)" in message
    assert "f-000" in message


def test_the_refused_fold_changes_neither_copy(project: Path) -> None:
    """A refusal leaves the aggregate at ten rows and every file where it was."""
    union, version = ledger.load(PROJECT, project)
    files_before = _run_files(project)

    with pytest.raises(ledger.LedgerError):
        ledger.write(PROJECT, union, version, project)

    assert len(_aggregate(project)["runs"]) == AGGREGATE_ROWS
    assert _run_files(project) == files_before
    assert "f-000" not in _aggregate_text(project)
    reread, _ = ledger.load(PROJECT, project)
    assert len(reread["runs"]) == AGGREGATE_ROWS + PER_RUN_FILES


def test_a_write_appending_one_new_run_is_accepted(project: Path) -> None:
    """The guard stays quiet on the shape it exists to permit.

    The aggregate's own rows plus one run that owns no file is the appending
    write: nothing already held as a file is folded in, so it is accepted and
    the fifty files are untouched.
    """
    _union, version = ledger.load(PROJECT, project)
    files_before = _run_files(project)
    before, _aggregate_version = ledger._load_aggregate(PROJECT, project)
    new_row = _row("r-new")

    accepted = ledger.write(
        PROJECT,
        {
            "members": [],
            "runs": [*before["runs"], new_row],
            "holds": [],
        },
        version,
        project,
    )

    assert accepted > version
    after = _aggregate(project)["runs"]
    assert [row["run_id"] for row in after] == [
        *(row["run_id"] for row in before["runs"]),
        "r-new",
    ]
    assert not ledger.run_path(PROJECT, "r-new", project).exists()
    assert _run_files(project) == files_before
    reread, _ = ledger.load(PROJECT, project)
    assert len(reread["runs"]) == AGGREGATE_ROWS + PER_RUN_FILES + 1


def test_a_hold_check_leaves_the_aggregate_row_count_unchanged(
    project: Path,
) -> None:
    """A hold transition is recorded without carrying any per-run row into it."""
    before = _aggregate(project)["runs"]
    files_before = _run_files(project)

    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=project,
    )

    assert [item["action"] for item in result["outcomes"]] == ["opened"]
    stored = _aggregate(project)
    assert [row["run_id"] for row in stored["runs"]] == [
        row["run_id"] for row in before
    ]
    assert [item["hold_id"] for item in stored["holds"]] == [
        "hold-alpha-20261017120000"
    ]
    assert _run_files(project) == files_before
    assert "f-000" not in _aggregate_text(project)
    reread, _ = ledger.load(PROJECT, project)
    assert len(reread["runs"]) == AGGREGATE_ROWS + PER_RUN_FILES
