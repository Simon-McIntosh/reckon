"""An identity file is judged only by the host whose kernel issued its pid."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims as claims
from reckon.crew import gc
from reckon.crew.node import CrewError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def crew_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home / "crew" / "claims"


def _dead_pid() -> int:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    return finished.pid


def _plant(store: Path, pid: int, host: str, token: str) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"exec-{pid}.identity"
    path.write_text(f"{host}\n{token}", encoding="utf-8")
    return path


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_an_other_hosts_file_inside_the_bound_is_kept(
    crew_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = _plant(crew_store, _dead_pid(), "another-host", "foreign-execution")

    report = gc.sweep_exec_identities(apply=True)

    assert report["kept"] == [str(foreign)]
    assert report["removed"] == []
    assert foreign.is_file()
    assert "removing identity file" not in capsys.readouterr().err


def test_an_other_hosts_file_beyond_the_bound_is_removed(
    crew_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    foreign = _plant(crew_store, _dead_pid(), "another-host", "foreign-execution")
    _age(foreign, gc.EXEC_IDENTITY_FOREIGN_HOST_MAX_AGE_SECONDS + 60.0)

    report = gc.sweep_exec_identities(apply=True)

    assert [entry["path"] for entry in report["removed"]] == [str(foreign)]
    assert report["kept"] == []
    assert not foreign.exists()
    assert f"removing identity file {foreign}" in capsys.readouterr().err


def test_this_hosts_file_for_a_dead_pid_is_removed(
    crew_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The pid is gone on this host, which is the only evidence a file that
    # names this host needs: its age never enters the decision.
    dead = _plant(crew_store, _dead_pid(), socket.gethostname(), "dead-execution")

    report = gc.sweep_exec_identities(apply=True)

    assert [entry["path"] for entry in report["removed"]] == [str(dead)]
    assert report["kept"] == []
    assert not dead.exists()
    assert f"removing identity file {dead}" in capsys.readouterr().err


def test_a_claim_held_from_another_host_is_not_reclaimed(crew_store: Path) -> None:
    crew_store.mkdir(parents=True, exist_ok=True)
    foreign_pid = _dead_pid()
    claim_path = claims._node_dispatch_claim_path(
        "identity-fixture", "same-worktree", "shared-node"
    )
    claim_path.write_text(
        json.dumps(
            {
                "run_id": "foreign-run",
                "project": "identity-fixture",
                "session": "another-session",
                "worktree_identity": "same-worktree",
                "node": "shared-node",
                "pid": foreign_pid,
                "process_start_time": "1",
                "exec_identity": "another-host\nforeign-execution",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    _plant(crew_store, foreign_pid, "another-host", "foreign-execution")

    with pytest.raises(CrewError, match="foreign-run"):
        claims._claim_node_dispatch(
            project="identity-fixture",
            worktree_identity="same-worktree",
            node_id="shared-node",
            run_id="second-run",
            session="same-session",
        )

    record = json.loads(claim_path.read_text(encoding="utf-8"))
    assert record["run_id"] == "foreign-run"
