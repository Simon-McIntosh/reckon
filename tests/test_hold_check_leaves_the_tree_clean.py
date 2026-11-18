"""A hold check writes only the run store, so no session inherits the tree dirty.

``record_hold_checks`` records the transition in the run store and touches no
committed path: the aggregate beside the run records keeps the bytes it had,
no run record is rewritten on its account, and HEAD does not move. Every case
here drives the hold path against a throwaway git repository and reads what
git and the store record: the tree is clean when the call returns, HEAD is
where it was, and the transition exists in the store. Every test is
hermetic — ``RECKON_HOME`` and ``RECKON_RUN_STORE`` move the crew home and its
store into a temp tree and the repository is built under ``tmp_path``, so
nothing here touches this workstation's own state.
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
    """Point the crew home and its run store at a temp tree.

    The store holds the roster and the budget holds, so it is the file a hold
    check writes; naming it explicitly keeps a case off this workstation's own
    store whatever the config home resolves to.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(config_home / "run_store.db"))
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
    file beside it, so a committing write has both copies to reconcile: the
    writer hands the run-file writer only the aggregate's rows. Two roster
    members are seeded so a case can retire one, and a hold check is given a
    record it must leave alone.
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


def _store_holds(repository: Path) -> list[dict]:
    return ledger.holds(PROJECT, repository)


def _head(repository: Path) -> str:
    return _git(repository, "rev-parse", "HEAD")


def test_closing_a_hold_leaves_the_tree_clean_and_changes_no_run_row(
    home, repository
) -> None:
    aggregate_before = _committed_ledger(repository)
    head_before = _head(repository)

    opened = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=repository,
    )
    assert [item["action"] for item in opened["outcomes"]] == ["opened"]
    assert [item["closed_at"] for item in _store_holds(repository)] == [None]

    closed = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": False}],
        checked_at=CLOSED_AT,
        root=repository,
    )
    assert [item["action"] for item in closed["outcomes"]] == ["closed"]

    assert _git(repository, "status", "--porcelain") == ""
    assert _head(repository) == head_before
    committed = _committed_ledger(repository)
    assert committed == aggregate_before
    assert [item["run_id"] for item in committed["runs"]] == ["r-one"]
    stored = _store_holds(repository)
    assert [(item["hold_id"], item["closed_at"]) for item in stored] == [
        (HOLD_ID, CLOSED_AT)
    ]
    assert [item["held_seconds"] for item in stored] == [600]


def test_opening_a_hold_leaves_the_tree_clean_and_changes_no_run_row(
    home, repository
) -> None:
    aggregate_before = _committed_ledger(repository)
    assert [item["run_id"] for item in aggregate_before["runs"]] == ["r-one"]
    head_before = _head(repository)

    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=repository,
    )

    assert [item["action"] for item in result["outcomes"]] == ["opened"]
    assert _git(repository, "status", "--porcelain") == ""
    assert _head(repository) == head_before
    committed = _committed_ledger(repository)
    assert committed == aggregate_before
    stored = _store_holds(repository)
    assert [item["hold_id"] for item in stored] == [HOLD_ID]
    assert [item["opened_at"] for item in stored] == [OPENED_AT]


def test_a_hold_check_makes_no_commit_while_a_merge_is_in_progress(
    home, repository
) -> None:
    _git(repository, "checkout", "-q", "-b", "side")
    (repository / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repository, "add", "--", "side.txt")
    _git(repository, "commit", "-q", "-m", "docs: add a side branch fixture")
    _git(repository, "checkout", "-q", "main")
    _git(repository, "merge", "--no-commit", "--no-ff", "-q", "side")
    assert (repository / ".git" / "MERGE_HEAD").exists()
    head_during_merge = _head(repository)
    status_during_merge = _git(repository, "status", "--porcelain")

    ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=repository,
    )

    assert _head(repository) == head_during_merge
    assert (repository / ".git" / "MERGE_HEAD").exists()
    assert _git(repository, "status", "--porcelain") == status_during_merge
    assert [item["hold_id"] for item in _store_holds(repository)] == [HOLD_ID]


def test_opening_a_hold_leaves_the_run_record_it_could_rewrite_alone(
    home, retired_record
) -> None:
    head_before = _head(retired_record)
    record_path = retired_record / RUN_RECORD_PATH
    record_before = record_path.read_bytes()
    aggregate_before = _committed_ledger(retired_record)

    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=retired_record,
    )

    assert [item["action"] for item in result["outcomes"]] == ["opened"]
    assert _git(retired_record, "status", "--porcelain") == ""
    assert _head(retired_record) == head_before
    assert record_path.read_bytes() == record_before
    assert RETIRED_KEY in json.loads(record_path.read_text(encoding="utf-8"))
    assert _committed_ledger(retired_record) == aggregate_before
    # The reader still maps the retired key on the way in, so a record left as
    # it was on disk answers the question the renamed field asks.
    data, _version = ledger.load_aggregate(PROJECT, retired_record)
    row = next(item for item in data["runs"] if item["run_id"] == "r-one")
    assert row["outside_scope_paths"] == []
    assert RETIRED_KEY not in row


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


def test_a_hold_check_that_changes_no_hold_commits_nothing(
    home, retired_record
) -> None:
    ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=OPENED_AT,
        root=retired_record,
    )
    head = _head(retired_record)

    result = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "alpha", "held": True}],
        checked_at=CLOSED_AT,
        root=retired_record,
    )

    assert [item["action"] for item in result["outcomes"]] == ["unchanged"]
    assert _head(retired_record) == head
    assert _git(retired_record, "status", "--porcelain") == ""
