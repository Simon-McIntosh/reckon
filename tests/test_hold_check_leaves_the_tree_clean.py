"""A hold check commits the aggregate it rewrites, so no session inherits it dirty.

``record_hold_checks`` writes the whole aggregate, which carries run rows
beside the holds, and the transition it records exists nowhere else. Every
case here drives the hold path against a throwaway git repository and reads
what git records: the tree is clean when the call returns, and the commit the
call made carries the hold without changing a run row. Every test is
hermetic — ``RECKON_HOME`` moves the crew home into a temp tree and the
repository is built under ``tmp_path``, so nothing here touches this
workstation's own state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from tests.helpers.git import git as _git

PROJECT = "reckon"
LEDGER_PATH = Path("docs") / "state" / PROJECT / "crew.json"
RUN_RECORD_PATH = Path("docs") / "state" / PROJECT / "runs" / "r-one.json"
OPENED_AT = "2026-10-03T05:00:00Z"
CLOSED_AT = "2026-10-03T05:10:00Z"
HOLD_ID = "hold-alpha-20261003050000"
RETIRED_KEY = "follow_on_paths"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point the crew home at a temp tree, leaving this workstation's alone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repository(tmp_path):
    """A throwaway git repository holding one promoted run row."""
    root = tmp_path / "repository"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    # Seed the aggregate's own run row directly, and a different run's file
    # beside it, so the ledger a case reads is split: the aggregate holds one
    # run row and runs/ holds another. Neither write folds a row the other holds
    # — a check that read the union and wrote it back would fold the file's row
    # into the aggregate, which is what the ledger writer refuses.
    ledger.write(
        PROJECT,
        {
            "members": [],
            "runs": [ledger.build_record(run_id="r-one", plan="plan-a", gate="passed")],
            "holds": [],
        },
        0,
        root=root,
    )
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id="r-two", plan="plan-a", gate="passed"),
        root=root,
    )
    _git(root, "add", "--", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed a promoted run")
    return root


@pytest.fixture()
def retired_record(repository: Path) -> Path:
    """The committed tree, plus one run record still carrying the retired key.

    The record is one of the aggregate's own ``runs`` rows, seeded with its own
    file beside it. ``record_hold_checks`` hands the run-file writer only the
    aggregate's rows, so a record held solely as a file is never visited and a
    case built on one would pass on a tree that never rewrote anything. Two
    roster members are seeded so a case can retire one.
    """
    path = repository / LEDGER_PATH
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["data"]["members"] = [{"id": "m-one"}, {"id": "m-two"}]
    # Both copies of the run carry the retired key, as a row promoted before the
    # rename does in both places: the reader maps each on the way in, so the two
    # agree, and only the re-encode on the way out differs from what is on disk.
    row = dict(envelope["data"]["runs"][0])
    row.pop("outside_scope_paths", None)
    row[RETIRED_KEY] = []
    envelope["data"]["runs"][0] = row
    path.write_text(
        json.dumps(envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    target = ledger.run_path(PROJECT, "r-one", repository)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ledger.serialize_run(row), encoding="utf-8")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed a row carrying the retired key")
    return repository


def _committed_ledger(repository: Path) -> dict:
    envelope = json.loads(_git(repository, "show", f"HEAD:{LEDGER_PATH.as_posix()}"))
    return envelope["data"]


def _commit_pending(repository: Path, message: str) -> None:
    """Commit whatever the fixture has left in the tree, so the next write's
    own commit is the only thing the following assertion reads."""
    if _git(repository, "status", "--porcelain"):
        _git(repository, "add", "--", "docs")
        _git(repository, "commit", "-q", "-m", message)


def test_closing_a_hold_leaves_the_tree_clean_and_changes_no_run_row(
    home, repository
) -> None:
    ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=repository,
    )
    _commit_pending(repository, "chore: fixture records the open hold")
    opened = _committed_ledger(repository)
    assert [item["run_id"] for item in opened["runs"]] == ["r-one"]
    assert [item["closed_at"] for item in opened["holds"]] == [None]

    ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": False}],
        checked_at=CLOSED_AT,
        root=repository,
    )

    assert _git(repository, "status", "--porcelain") == ""
    committed = _committed_ledger(repository)
    assert committed["runs"] == opened["runs"]
    assert [item["closed_at"] for item in committed["holds"]] == [CLOSED_AT]
    assert (
        _git(repository, "log", "-1", "--format=%s") == f"chore(holds): close {HOLD_ID}"
    )


def test_opening_a_hold_leaves_the_tree_clean_and_changes_no_run_row(
    home, repository
) -> None:
    runs_before = _committed_ledger(repository)["runs"]
    assert [item["run_id"] for item in runs_before] == ["r-one"]

    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=repository,
    )

    assert [item["action"] for item in result["outcomes"]] == ["opened"]
    assert _git(repository, "status", "--porcelain") == ""
    committed = _committed_ledger(repository)
    assert committed["runs"] == runs_before
    assert [item["hold_id"] for item in committed["holds"]] == [HOLD_ID]
    assert (
        _git(repository, "log", "-1", "--format=%s") == f"chore(holds): open {HOLD_ID}"
    )


def test_a_hold_check_makes_no_commit_while_a_merge_is_in_progress(
    home, repository, capsys
) -> None:
    _git(repository, "checkout", "-q", "-b", "side")
    (repository / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repository, "add", "--", "side.txt")
    _git(repository, "commit", "-q", "-m", "docs: add a side branch fixture")
    _git(repository, "checkout", "-q", "main")
    _git(repository, "merge", "--no-commit", "--no-ff", "-q", "side")
    assert (repository / ".git" / "MERGE_HEAD").exists()
    head_during_merge = _git(repository, "rev-parse", "HEAD")

    ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=repository,
    )

    assert _git(repository, "rev-parse", "HEAD") == head_during_merge
    assert (repository / ".git" / "MERGE_HEAD").exists()
    assert "merge in progress" in capsys.readouterr().err
    assert LEDGER_PATH.as_posix() in _git(repository, "status", "--porcelain")
    assert [item["hold_id"] for item in ledger.holds(PROJECT, repository)] == [HOLD_ID]


def test_opening_a_hold_commits_the_run_record_it_rewrote(home, retired_record) -> None:
    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=retired_record,
    )

    assert [item["action"] for item in result["outcomes"]] == ["opened"]
    assert _git(retired_record, "status", "--porcelain") == ""
    committed_files = _git(
        retired_record, "show", "--name-only", "--format="
    ).splitlines()
    assert RUN_RECORD_PATH.as_posix() in committed_files
    rewritten = json.loads(
        _git(retired_record, "show", f"HEAD:{RUN_RECORD_PATH.as_posix()}")
    )
    assert RETIRED_KEY not in rewritten
    assert rewritten["outside_scope_paths"] == []


def test_a_committing_write_commits_the_run_record_it_rewrote(
    home, retired_record
) -> None:
    data, version = ledger.load_aggregate(PROJECT, retired_record)
    assert isinstance(version, int)

    returned = ledger.write(
        PROJECT,
        {
            "members": [{"id": "m-two"}],
            "runs": data["runs"],
            "holds": data["holds"],
        },
        version,
        retired_record,
        allow_member_removal=True,
        commit=True,
    )

    assert isinstance(returned, int)
    assert returned > version
    assert _git(retired_record, "status", "--porcelain") == ""
    committed_files = _git(
        retired_record, "show", "--name-only", "--format="
    ).splitlines()
    assert RUN_RECORD_PATH.as_posix() in committed_files
    assert _git(retired_record, "log", "-1", "--format=%s") == (
        "chore(roster): retire m-one"
    )


def test_a_hold_check_that_changes_no_hold_commits_nothing(home, retired_record) -> None:
    ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=retired_record,
    )
    head = _git(retired_record, "rev-parse", "HEAD")

    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=CLOSED_AT,
        root=retired_record,
    )

    assert [item["action"] for item in result["outcomes"]] == ["unchanged"]
    assert _git(retired_record, "rev-parse", "HEAD") == head
    assert _git(retired_record, "status", "--porcelain") == ""
