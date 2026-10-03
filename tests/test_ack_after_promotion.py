"""A deliberate remainder of a promoted run can be acknowledged.

A worktree-held obligation outlives the run's live pointer: the run is
promoted, its record says the tree was retained, and the obligation reader
raises the duty from that record. ``crew ack`` wrote only to the live pointer,
so the duty could not be deferred once the run was promoted and the stop hook
blocked every turn on a remainder the coordinator holds on purpose.

Each case here promotes a run whose tree is still held, acknowledges it with a
reason, and reads the obligations back. Every crew directory is
environment-resolved to a temporary config home, and the synthesised
repository and ledger the fixture writes are left byte-for-byte identical
across the read.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "ack-after-promotion-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
COMPLETED_AT = OBSERVED_AT - timedelta(minutes=10)
NODE = "promoted-remainder"
RUN_ID = f"r-20261003T060000000000-{NODE}"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the acknowledgement fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(
        runs,
        "drain",
        lambda project, session=None: {
            "project": project,
            "session": session,
            "unreconciled_runs": 1,
        },
    )
    return root


def _worktree(root: Path, node: str) -> Path:
    """Register a tree the way a dispatched run leaves one behind."""
    path = root.parent / "managed-worktrees" / SESSION / node
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _promote(root: Path, worktree: Path) -> dict[str, Any]:
    """Publish one promoted run whose retained tree is still held."""
    record = ledger.build_record(
        run_id=RUN_ID,
        plan="fixture-plan",
        gate="failed",
        node=NODE,
        completed_at=COMPLETED_AT.isoformat(),
    )
    record["worktree_retention"] = {
        "classification": "retained-for-resume",
        "worktree": str(worktree.resolve()),
        "session_id": "fixture-session",
        "session_source": "pointer",
        "retained_at": COMPLETED_AT.isoformat(),
    }
    ledger.append_run(PROJECT, record, root=root, allow_create=True)
    return record


def _until_instant(*, hours: int) -> str:
    return (OBSERVED_AT + timedelta(hours=hours)).isoformat()


def _owed(result: dict[str, Any]) -> list[str]:
    return [str(item["run_id"]) for item in result["obligations"]]


def test_a_promoted_remainder_is_acknowledged_and_leaves_the_stop_path(
    fleet: Path,
) -> None:
    """The duty is owed first, and a recorded reason defers it."""
    tree = _worktree(fleet, NODE)
    _promote(fleet, tree)
    assert runs.pointer_path(RUN_ID).exists() is False

    # The duty the acknowledgement is about is shown to be there before it is
    # excused: an acknowledgement that defers nothing proves nothing.
    before = obligations_module.obligations(PROJECT, SESSION)
    assert _owed(before) == [RUN_ID]
    assert before["acknowledged"] == []

    reason = "the successor run's lineage is what still references this tree"
    runs.record_run_acknowledgement(RUN_ID, reason, _until_instant(hours=1))

    result = obligations_module.obligations(PROJECT, SESSION)

    assert _owed(result) == []
    assert [item["run_id"] for item in result["acknowledged"]] == [RUN_ID]
    deferred = result["acknowledged"][0]
    assert deferred["kind"] == "worktree-held"
    assert deferred["reason"] == reason
    assert deferred["until"] == _until_instant(hours=1)

    # The deferral is durable in the promoted run's own store, and the ledger
    # still reads as one history rather than two disagreeing copies.
    stored = ledger.load(PROJECT, root=fleet)[0]
    row = next(record for record in stored["runs"] if record.get("run_id") == RUN_ID)
    assert row["acknowledgement"]["reason"] == reason


def test_the_duty_returns_once_the_acknowledgement_expires(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past its ``until`` the deferral is not in force, and the duty returns."""
    tree = _worktree(fleet, NODE)
    _promote(fleet, tree)
    runs.record_run_acknowledgement(RUN_ID, "short deferral", _until_instant(hours=1))
    assert obligations_module.obligations(PROJECT, SESSION)["acknowledged"] != []

    # The reader's clock moves past the deferral's own instant, derived from
    # the fixture rather than written down, with no second write anywhere.
    monkeypatch.setattr(
        obligations_module, "_utc_now", lambda: OBSERVED_AT + timedelta(hours=2)
    )
    result = obligations_module.obligations(PROJECT, SESSION)

    assert _owed(result) == [RUN_ID]
    assert result["acknowledged"] == []


def test_an_acknowledgement_with_no_reason_is_refused(fleet: Path) -> None:
    """No reason means no deferral, for a promoted run as for a live one."""
    tree = _worktree(fleet, NODE)
    _promote(fleet, tree)

    with pytest.raises(runs.CrewError, match="reason"):
        runs.record_run_acknowledgement(RUN_ID, "   ", _until_instant(hours=1))

    data = ledger.load(PROJECT, root=fleet)[0]
    assert data["runs"], "the ledger is shown to carry the run it did not amend"
    row = next(item for item in data["runs"] if item.get("run_id") == RUN_ID)
    assert "acknowledgement" not in row
