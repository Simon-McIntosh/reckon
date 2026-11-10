"""A producer left on older code is a producer to cycle, not one reloading."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import obligation_snapshot, runs
from tests.helpers.git import git

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "stale-code-producer"
SESSION = "stale-code-session"
OLD_STAMP = "older-code-stamp"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(path))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(path / "mounts.json"))
    return path


@pytest.fixture
def repository(tmp_path: Path, home: Path) -> Path:
    path = tmp_path / "repo"
    (path / "docs" / "state" / PROJECT).mkdir(parents=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed fixture"),
    ):
        git(path, *args)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(path / "docs")}), encoding="utf-8"
    )
    with runs.follower_registration(PROJECT, SESSION):
        pass
    return path


def _document(*, age_seconds: float) -> dict[str, Any]:
    """A snapshot from a live producer whose code stamp is older than the checkout.

    The recorded pid is this test process, alive for the run and recorded with
    its own start time, so the producer reads as live and the older stamp is
    what makes the reading ``STALE_CODE``.
    """
    pid = os.getpid()
    computed = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    return {
        "computed_at": computed.isoformat(),
        "producer": {
            "pid": pid,
            "pid_start_time": str(obligation_snapshot.process_start_time(pid)),
            "code_stamp": OLD_STAMP,
        },
        "obligations": {},
    }


def test_stale_code_snapshot_inside_the_window_reads_as_a_reload() -> None:
    document = _document(age_seconds=5)

    assert obligation_snapshot.reload_in_progress(
        document, state=obligation_snapshot.STALE_CODE
    )


def test_stale_code_snapshot_past_the_window_does_not_read_as_a_reload() -> None:
    window = obligation_snapshot.producer_reload_window_seconds()
    document = _document(age_seconds=window + 60)

    assert not obligation_snapshot.reload_in_progress(
        document, state=obligation_snapshot.STALE_CODE
    )


def test_hook_names_old_code_rather_than_a_reload_for_an_aged_stale_snapshot(
    repository: Path,
) -> None:
    document = _document(age_seconds=20 * 3_600)
    assert (
        obligation_snapshot.freshness(document, project=PROJECT)
        == obligation_snapshot.STALE_CODE
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)

    env = dict(os.environ)
    run = subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(
            {
                "session_id": SESSION,
                "cwd": str(repository),
                "hook_event_name": "UserPromptSubmit",
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert not run.stderr, run.stderr
    line = json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "the producer is running older code than the checkout" in line, line
    assert "producer reloading" not in line, line