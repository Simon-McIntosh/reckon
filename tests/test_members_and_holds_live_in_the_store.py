"""Roster mutations and budget holds survive without a committed aggregate."""

import json
import sqlite3
import subprocess
from datetime import UTC, datetime

import pytest

from reckon import _store, budget, ledger, run_store
from reckon.crew import promotion_records, routing

PROJECT = "fixture-project"
NOW = datetime(2026, 5, 20, tzinfo=UTC)


@pytest.fixture
def roster(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "run_store.db"))
    monkeypatch.setattr(routing, "list_live", lambda **kwargs: [])
    path = ledger.ledger_path(PROJECT, root)
    path.parent.mkdir(parents=True)
    members = [
        {"id": "permanent", "harness": "test", "created": "2026-01-01"},
        {"id": "session-idle", "harness": "test", "created": "2026-01-01"},
    ]
    holds = [{"hold_id": "historic", "backend": "other", "closed_at": "2026-01-02"}]
    path.write_text(
        json.dumps(
            {"data": {"_version": 7, "members": members, "holds": holds, "runs": []}}
        )
    )
    for args in (
        ["init", "-q"],
        ["add", str(path.relative_to(root))],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "test: seed roster\n\nKeep the fixture aggregate tracked.",
        ],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root, path


def _snapshot(root):
    data, version = ledger.load(PROJECT, root=root)
    return data["members"], version, data["holds"]


def _tracked(root):
    paths = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"], text=True
    ).split("\0")
    return {name: (root / name).read_bytes() for name in paths if name}


@pytest.mark.parametrize("operation", ["register", "reap", "hold"])
def test_mutation_writes_no_committed_path(roster, operation):
    root, path = roster
    before = _tracked(root)
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"])
    if operation == "register":
        entry = ledger.register_member(
            PROJECT, "registered", harness="test", root=root, commit=True
        )
        assert entry["id"] == "registered"
        assert ledger.member(PROJECT, "registered", root=root) == entry
    elif operation == "reap":
        result = routing.reap_idle_session_members(PROJECT, root=root, now=NOW)
        assert result["reaped"] == ["session-idle"]
        assert [row["id"] for row in ledger.members(PROJECT, root)] == ["permanent"]
    else:
        result = budget.record_checks(
            PROJECT,
            [{"backend": "test", "held": True, "reason": "fixture limit"}],
            root=root,
            now=NOW,
        )
        assert result["outcomes"][0]["action"] == "opened"
        assert ledger.holds(PROJECT, root)[-1]["reason"] == "fixture limit"
    assert _tracked(root) == before, (
        "a roster or hold mutation rewrote a committed path"
    )
    assert (
        subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]) == head
    )
    expected = _snapshot(root)
    assert expected[1] == 8
    path.unlink()
    assert _snapshot(root) == expected


def test_imported_roster_version_and_holds_survive_aggregate_removal(roster):
    root, path = roster
    expected = _snapshot(root)
    assert expected[0] and expected[2] and expected[1] == 7
    path.unlink()
    assert _snapshot(root) == expected


def test_holds_deduplicate_and_close_without_writing_history(roster):
    root, path = roster
    before = _tracked(root)
    held = [{"backend": "test", "held": True}]
    opened = ledger.record_hold_checks(
        PROJECT, held, root=root, checked_at="2026-05-20T00:00:00Z"
    )
    unchanged = ledger.record_hold_checks(
        PROJECT, held, root=root, checked_at="2026-05-20T00:01:00Z"
    )
    assert unchanged["version"] == opened["version"] == 8
    assert unchanged["outcomes"][0]["action"] == "unchanged"
    closed = ledger.record_hold_checks(
        PROJECT,
        [{"backend": "test", "held": False, "resumption_fired": True}],
        root=root,
        checked_at="2026-05-20T00:01:30Z",
    )
    assert closed["version"] == 9
    assert closed["outcomes"][0]["hold"]["held_seconds"] == 90
    assert closed["outcomes"][0]["hold"]["resumption_fired"] is True
    assert _tracked(root) == before
    expected = _snapshot(root)
    path.unlink()
    assert _snapshot(root) == expected


def test_projects_can_share_member_and_hold_ids(roster):
    root, _path = roster
    first = _snapshot(root)
    second = "another-project"
    other = ledger.register_member(second, "permanent", harness="different", root=root)
    assert other["harness"] == "different"
    assert _snapshot(root) == first
    ledger.record_hold_checks(
        PROJECT,
        [{"backend": "test", "held": True}],
        root=root,
        checked_at="2026-05-20T00:00:00Z",
    )
    ledger.record_hold_checks(
        second,
        [{"backend": "test", "held": True, "reason": "different"}],
        root=root,
        checked_at="2026-05-20T00:00:00Z",
    )
    left = ledger.holds(PROJECT, root)[-1]
    right = ledger.holds(second, root)[-1]
    assert left["hold_id"] == right["hold_id"]
    assert left["reason"] == ""
    assert right["reason"] == "different"


def test_empty_roster_is_not_reimported_from_another_checkout(roster, tmp_path):
    root, path = roster
    stale_root = tmp_path / "stale-checkout"
    stale_path = ledger.ledger_path(PROJECT, stale_root)
    stale_path.parent.mkdir(parents=True)
    stale_path.write_bytes(path.read_bytes())
    data, version = ledger.load_auxiliary(PROJECT, root)
    new_version = ledger.write_members(
        PROJECT, [], version, root, allow_member_removal=True
    )
    assert ledger.load_auxiliary(PROJECT, stale_root) == (
        {"members": [], "holds": data["holds"]},
        new_version,
    )
    path.unlink()
    assert ledger.members(PROJECT, root) == []


def test_stale_roster_and_undeclared_removal_are_refused(roster):
    root, _path = roster
    data, version = ledger.load_auxiliary(PROJECT, root)
    with pytest.raises(ledger.LedgerError, match="allow_member_removal=True"):
        ledger.write_members(PROJECT, [], version, root)
    assert ledger.load_auxiliary(PROJECT, root) == (data, version)
    ledger.register_member(PROJECT, "another", harness="test", root=root)
    with pytest.raises(ledger.LedgerError, match="version changed"):
        ledger.write_members(PROJECT, data["members"], version, root)
    with run_store.RunStore() as store, pytest.raises(_store.VersionConflict):
        store.write_auxiliary(PROJECT, {"holds": []}, version)
    assert ledger.member(PROJECT, "another", root=root)
    assert ledger.holds(PROJECT, root) == data["holds"]


def test_failed_collection_replace_rolls_back_rows_and_version(roster):
    root, _path = roster
    data, version = ledger.load_auxiliary(PROJECT, root)
    with run_store.RunStore() as store, pytest.raises(sqlite3.IntegrityError):
        store.write_auxiliary(
            PROJECT,
            {"members": [{"id": "duplicate"}, {"id": "duplicate"}]},
            version,
        )
    assert ledger.load_auxiliary(PROJECT, root) == (data, version)


def test_disposable_retirement_changes_only_the_store(roster):
    root, _path = roster
    run_id = "fixture-run"
    member_id = "disposable-" + run_id
    ledger.register_member(PROJECT, member_id, harness="test", root=root)
    before = _tracked(root)
    receipt = promotion_records._retire_disposable_identity(
        {"project": PROJECT, "repo": str(root), "run_id": run_id, "member": member_id}
    )
    assert receipt == {"identity_retired": True, "identity_member": member_id}
    assert ledger.member(PROJECT, member_id, root=root) is None
    assert _tracked(root) == before


def test_store_failure_never_falls_back_to_committed_writes(roster, monkeypatch):
    root, _path = roster
    before = _tracked(root)

    def refuse(*args, **kwargs):
        raise sqlite3.DatabaseError("fixture store unavailable")

    monkeypatch.setattr(run_store.RunStore, "write_auxiliary", refuse)
    with pytest.raises(sqlite3.DatabaseError, match="store unavailable"):
        ledger.register_member(
            PROJECT, "registered", harness="test", root=root, commit=True
        )
    assert ledger.member(PROJECT, "registered", root=root) is None
    assert _tracked(root) == before


def test_unscoped_auxiliary_tables_migrate_without_losing_payloads(tmp_path):
    path = tmp_path / "store.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE members (member_id TEXT PRIMARY KEY, payload TEXT NOT NULL); CREATE TABLE holds (hold_id TEXT PRIMARY KEY, payload TEXT NOT NULL);"
        )
        connection.execute(
            "INSERT INTO members VALUES (?, ?)", ("member", '{"id":"member"}')
        )
        connection.execute(
            "INSERT INTO holds VALUES (?, ?)", ("hold", '{"hold_id":"hold"}')
        )
    with run_store.RunStore(path) as store:
        assert store._auxiliary_rows("") == {
            "members": [{"id": "member"}],
            "holds": [{"hold_id": "hold"}],
        }
        store.append_member({"id": "member", "harness": "test"}, project="scoped")
    with run_store.RunStore(path) as store:
        assert store._auxiliary_rows("scoped")["members"] == [
            {"id": "member", "harness": "test"}
        ]
        assert store._auxiliary_rows("")["members"] == [{"id": "member"}]
