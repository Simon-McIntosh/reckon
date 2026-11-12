"""A promotion that ended before its ledger row is retried by the landing pass.

``crew complete --land`` writes a pending-landing record and then promotes. On
12 November a promotion was cut off between the record and the ledger row: the
station took the promotion lease, wrote the record with ``attempts: 0``, and
then stopped. The lease went stale, no promote commit existed, and the run's
pointer stayed live. ``finish_pending_landings`` did nothing with the record
because no promoted row stood behind it, and the landing pass refused the run
because its recorded attempt never produced a receipt, so nothing retried the
promotion until a coordinator promoted it by hand.

The pass now reads a record with a live run, no promoted row, no lease holder
and no fresh write as a promotion that ended between the two, retires the
record and the interrupted attempt, and leaves the run promotable again.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import merge, pending_landing, promotion, runs
from reckon.crew.host_lease import HostLease
from reckon.crew.runs import _write_json, pointer_path, read_pointer

PROJECT = "unfinished-promotion-fixture"
RUN_ID = "r-20261112T210600000000-unfinished-promotion"
_REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def the_real_pointer_directory_is_not_a_fixture_target() -> None:
    real_pointer = _REAL_LIVE / f"{RUN_ID}.json"
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return tmp_path


def _repository(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout the ledger resolves against, with one commit on its branch."""
    repo = home / "repo"
    _git(home, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "chore: base")
    monkeypatch.setattr(merge, "project_repository", lambda project: repo)
    return repo


def _live_pointer(repository: Path, *, status: str = "attempting") -> None:
    """Write the live pointer a station leaves when it launches a land step."""
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "attempt": 2,
            "phase": "delivered",
            "launcher_host": socket.gethostname().split(".")[0],
            "pid": os.getpid(),
            "created_at": "2026-11-12T20:06:00Z",
            "manifest_path": "/durable/manifest.md",
            "admission_landing": {
                "head": "a" * 40,
                "guard": "crew land",
                "status": status,
                "code_stamp": "stamp",
                "review_stamp": "review",
                "attempts": 0,
                "failed_at": 0.0,
            },
            "node": {
                "id": "an-unfinished-promotion-is-retried",
                "plan": "the-monitor-reads-each-fact-once",
                "section": "s39",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


def _unfinished(home: Path, monkeypatch: pytest.MonkeyPatch, *, age: float) -> Path:
    """The state a promotion leaves when it stops before writing its ledger row."""
    repository = _repository(home, monkeypatch)
    _live_pointer(repository)
    pending_landing.write_pending(RUN_ID, PROJECT, now=1000.0 - age)
    return repository


def _claim(home: Path) -> HostLease:
    lease = HostLease(
        runs.crew_home(),
        promotion._promotion_lease_name(RUN_ID),
        socket.gethostname(),
        os.getpid(),
        "",
    )
    assert lease.claim(), "the fixture's lease could not be claimed"
    return lease


def _sweep(now: float) -> list[dict[str, Any]]:
    return pending_landing.finish_pending_landings(now=now)


def test_an_unfinished_promotion_is_retried_and_its_record_cleared(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unfinished(home, monkeypatch, age=10 * 60)

    results = _sweep(now=1000.0)

    assert results == [
        {"run_id": RUN_ID, "status": "retried-unfinished", "attempt_retired": True}
    ], results
    assert not list(pending_landing.pending_dir().glob("*.json"))
    retained = read_pointer(RUN_ID)
    assert retained.get("run_id") == RUN_ID
    assert retained["admission_landing"]["status"] == "interrupted"
    assert [row["run_id"] for row in runs.list_live()] == [RUN_ID]

    # One pass is the whole retry: nothing is left for a later pass to do.
    assert _sweep(now=1000.0 + 60) == []


def test_a_record_whose_promotion_still_holds_its_lease_is_left_alone(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unfinished(home, monkeypatch, age=10 * 60)
    lease = _claim(home)
    try:
        assert _sweep(now=1000.0) == []
        assert [path.stem for path in pending_landing.pending_dir().glob("*.json")] == [
            RUN_ID
        ]
        assert read_pointer(RUN_ID)["admission_landing"]["status"] == "attempting"
    finally:
        lease.release()


def test_a_record_written_moments_ago_is_not_yet_treated_as_ended(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion takes its lease seconds after writing the record."""
    _unfinished(home, monkeypatch, age=1.0)

    assert _sweep(now=1000.0) == []

    assert [path.stem for path in pending_landing.pending_dir().glob("*.json")] == [
        RUN_ID
    ]
    assert read_pointer(RUN_ID)["admission_landing"]["status"] == "attempting"


def test_a_record_whose_run_is_gone_is_still_dropped_when_it_is_old(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unfinished(home, monkeypatch, age=10 * 3600)
    pointer_path(RUN_ID).unlink()

    results = _sweep(now=1000.0)

    assert results == [{"run_id": RUN_ID, "status": "dropped-unpromoted"}]
    assert not list(pending_landing.pending_dir().glob("*.json"))
