"""An expired acknowledgement file is swept by the reader that finds it.

``recorded_promoted_acknowledgements`` reads every deferral file under the
crew home on each obligations read, and nothing removed one once its ``until``
had passed, so the directory grew and every expired deferral was parsed on
every read. The reader now removes a file whose ``until`` has passed as it
reads it, judged against the same clock the obligations reader honours a
deferral by, so a record is swept exactly when it stops excusing the duty it
named.

Each case works in a temporary config home: promoted runs keep their
worktrees, and the obligations read is shown to sweep an expired deferral
while still honouring a current one.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "expired-acknowledgement-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
COMPLETED_AT = OBSERVED_AT - timedelta(minutes=10)
EXPIRED_NODE = "expired-remainder"
CURRENT_NODE = "current-remainder"
RUN_EXPIRED = f"r-20261003T060000000000-{EXPIRED_NODE}"
RUN_CURRENT = f"r-20261003T060000000000-{CURRENT_NODE}"


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


def _promote(root: Path, worktree: Path, run_id: str, node: str) -> None:
    """Publish one promoted run whose retained tree is still held."""
    record = ledger.build_record(
        run_id=run_id,
        plan="fixture-plan",
        gate="failed",
        node=node,
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


def _owed(result: dict[str, Any]) -> list[str]:
    return sorted(str(item["run_id"]) for item in result["obligations"])


def test_an_expired_acknowledgement_is_swept_and_a_current_one_honoured(
    fleet: Path,
) -> None:
    """The expired file is removed by the read; the current one still defers."""
    _promote(fleet, _worktree(fleet, EXPIRED_NODE), RUN_EXPIRED, EXPIRED_NODE)
    _promote(fleet, _worktree(fleet, CURRENT_NODE), RUN_CURRENT, CURRENT_NODE)

    # Both duties are owed before either is deferred, so the case shows the
    # acknowledgement reader at work rather than an empty obligation list.
    before = obligations_module.obligations(PROJECT, SESSION)
    assert _owed(before) == sorted([RUN_EXPIRED, RUN_CURRENT])

    runs.record_run_acknowledgement(
        RUN_EXPIRED,
        "a deferral whose instant has already passed",
        (OBSERVED_AT - timedelta(hours=1)).isoformat(),
    )
    runs.record_run_acknowledgement(
        RUN_CURRENT,
        "a deferral still in force",
        (OBSERVED_AT + timedelta(hours=1)).isoformat(),
    )
    expired_path = runs.acknowledgement_path(RUN_EXPIRED)
    current_path = runs.acknowledgement_path(RUN_CURRENT)
    assert expired_path.is_file()
    assert current_path.is_file()

    result = obligations_module.obligations(PROJECT, SESSION)

    assert expired_path.exists() is False
    assert current_path.is_file()
    assert _owed(result) == [RUN_EXPIRED]
    acknowledged = [item["run_id"] for item in result["acknowledged"]]
    assert acknowledged == [RUN_CURRENT]
    deferred = result["acknowledged"][0]
    assert deferred["reason"] == "a deferral still in force"
    assert deferred["until"] == (OBSERVED_AT + timedelta(hours=1)).isoformat()


def test_a_deferral_with_an_unreadable_instant_is_left_in_place(fleet: Path) -> None:
    """A sweep removes what it can prove expired, and guesses at nothing else."""
    _promote(fleet, _worktree(fleet, EXPIRED_NODE), RUN_EXPIRED, EXPIRED_NODE)
    path = runs.acknowledgement_path(RUN_EXPIRED)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": RUN_EXPIRED,
                "project": PROJECT,
                "reason": "an instant no reader can judge",
                "until": "next tuesday",
                "recorded_at": OBSERVED_AT.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    assert path.is_file()
    assert _owed(result) == [RUN_EXPIRED]
    assert result["acknowledged"] == []


def test_the_ack_help_names_a_promoted_run() -> None:
    """The command's own help tells the operator a promoted run is accepted."""
    result = CliRunner().invoke(cli_main, ["crew", "ack", "--help"])

    assert result.exit_code == 0, result.output
    # Both the command's description and its --run help carry the word, so
    # the option and the docstring are each shown to name the promoted path.
    assert result.output.count("promoted") >= 2, result.output
