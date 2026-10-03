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
import subprocess
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "reckon"
LEDGER_PATH = Path("docs") / "state" / PROJECT / "crew.json"
OPENED_AT = "2026-10-03T05:00:00Z"
CLOSED_AT = "2026-10-03T05:10:00Z"
HOLD_ID = "hold-alpha-20261003050000"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id="r-one", plan="plan-a", gate="passed"),
        root=root,
    )
    # Fold the seeded run file into the aggregate and commit it, so the
    # aggregate every case reads already carries a run row and a clean tree.
    data, version = ledger.load(PROJECT, root)
    ledger.write(PROJECT, data, version, root)
    _git(root, "add", "--", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed a promoted run")
    return root


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
