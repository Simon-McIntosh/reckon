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
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reckon import ledger
from tests.helpers.git import git as _git

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


@pytest.fixture()
def crew_home(tmp_path, monkeypatch) -> Path:
    """Move the crew home into the temp tree, leaving this workstation's alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def git_project(project: Path, crew_home: Path) -> Path:
    """The split-state project as a git checkout the writers can commit in."""
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "worker@example.invalid")
    _git(project, "config", "user.name", "Worker")
    _git(project, "add", "--", "docs")
    _git(project, "commit", "-q", "-m", "chore: seed the split ledger")
    return project


def _seed_member(root: Path, member_id: str, created: str) -> None:
    """Add one roster row to the aggregate's own members, never the union's."""
    data, version = ledger.load_aggregate(PROJECT, root)
    data["members"] = [*data["members"], {"id": member_id, "created": created}]
    ledger.write(PROJECT, data, version, root)
    _git(root, "add", "--", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed a roster row")


def _assert_aggregate_untouched(root: Path, files_before: dict[str, bytes]) -> None:
    stored = _aggregate(root)
    assert [row["run_id"] for row in stored["runs"]] == [
        f"a-{index:03d}" for index in range(AGGREGATE_ROWS)
    ]
    assert _run_files(root) == files_before
    assert "f-000" not in _aggregate_text(root)


def _assert_aggregate_rows_unchanged(root: Path) -> None:
    """The aggregate's row set is unchanged and every per-run file still stands."""
    assert [row["run_id"] for row in _aggregate(root)["runs"]] == [
        f"a-{index:03d}" for index in range(AGGREGATE_ROWS)
    ]
    assert len(_run_files(root)) == PER_RUN_FILES
    assert "f-000" not in _aggregate_text(root)


def test_the_idle_reap_leaves_the_aggregate_row_count_unchanged(
    git_project: Path,
) -> None:
    """The reap writes the aggregate's own rows with its roster removal applied."""
    from reckon.crew import routing

    _seed_member(git_project, "session-idle-one", "2020-01-01T00:00:00Z")
    files_before = _run_files(git_project)

    result = routing.reap_idle_session_members(
        PROJECT,
        root=git_project,
        now=datetime(2026, 10, 17, tzinfo=timezone.utc),
    )

    assert result["reaped"] == ["session-idle-one"]
    assert _aggregate(git_project)["members"] == []
    _assert_aggregate_untouched(git_project, files_before)


def test_the_identity_retirement_leaves_the_aggregate_row_count_unchanged(
    git_project: Path,
) -> None:
    """Retiring a run's disposable identity carries no per-run row with it."""
    from reckon.crew import promotion_records, routing

    run_id = "a-000"
    member_id = routing._disposable_member_id(run_id)
    _seed_member(git_project, member_id, "2026-10-01T00:00:00Z")
    files_before = _run_files(git_project)

    result = promotion_records._retire_disposable_identity(
        {
            "run_id": run_id,
            "member": member_id,
            "project": PROJECT,
            "repo": str(git_project),
        }
    )

    assert result.get("identity_retired") is True, result
    assert _aggregate(git_project)["members"] == []
    _assert_aggregate_untouched(git_project, files_before)


def test_the_gate_rerun_record_leaves_the_aggregate_row_count_unchanged(
    git_project: Path,
) -> None:
    """The re-run records its report on the run's aggregate row, beside the files."""
    from reckon.crew import promotion_gate

    files_before = _run_files(git_project)

    promotion_gate.record_gate_rerun_at_integrated_revision(
        project=PROJECT,
        run_id="a-000",
        repository=git_project,
        root=git_project,
        command="true",
    )

    stored = _aggregate(git_project)
    assert stored["runs"][0].get("integrated_gate_check"), "the report was not recorded"
    _assert_aggregate_untouched(git_project, files_before)


def test_the_figure_backfill_leaves_the_aggregate_row_count_unchanged(
    git_project: Path,
) -> None:
    """A backfill patches each row where it is held, aggregate or file."""
    from scripts.derive_run_figures import backfill_run_figures

    backfill_run_figures(PROJECT, root=git_project)

    _assert_aggregate_rows_unchanged(git_project)


def test_the_duration_backfill_leaves_the_aggregate_row_count_unchanged(
    git_project: Path,
) -> None:
    """A duration backfill patches each row where it is held, aggregate or file."""
    from scripts.backfill_run_durations import backfill_run_durations

    backfill_run_durations(PROJECT, root=git_project)

    _assert_aggregate_rows_unchanged(git_project)
