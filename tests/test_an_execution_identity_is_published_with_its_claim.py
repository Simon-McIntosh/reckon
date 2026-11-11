"""An execution publishes its identity only for as long as it holds a claim."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def _identities(store: Path) -> list[Path]:
    return sorted(store.glob("exec-*.identity"))


def _run_imported(source: str, store: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RECKON_HOME": str(store.parents[1]),
            "PYTHONPATH": str(ROOT),
        },
    )


def _take_claim(run_id: str) -> claims._NodeDispatchClaim:
    return claims._claim_node_dispatch(
        project="identity-fixture",
        worktree_identity="same-worktree",
        node_id="same-node",
        run_id=run_id,
        session="same-session",
    )


def test_importing_the_module_publishes_no_identity(crew_store: Path) -> None:
    imported = _run_imported(
        "import os; import reckon.crew.dispatch_claims; print(os.getpid())",
        crew_store,
    )
    pid = int(imported.stdout)
    assert not (crew_store / f"exec-{pid}.identity").exists()
    assert _identities(crew_store) == []


def test_a_taken_and_released_claim_leaves_no_identity(crew_store: Path) -> None:
    ran = _run_imported(
        "import os\n"
        "from reckon.crew import dispatch_claims as claims\n"
        "held = claims._claim_node_dispatch(\n"
        "    project='identity-fixture',\n"
        "    worktree_identity='same-worktree',\n"
        "    node_id='subprocess-node',\n"
        "    run_id='subprocess-run',\n"
        "    session='same-session',\n"
        ")\n"
        "print(os.getpid(), held.path.is_file())\n"
        "held.release()\n",
        crew_store,
    )
    pid, field_claim = ran.stdout.split()
    assert field_claim == "True"
    assert not (crew_store / f"exec-{pid}.identity").exists()
    assert _identities(crew_store) == []


def test_a_live_holder_is_still_refused(crew_store: Path) -> None:
    first = _take_claim("first-run")
    identity_file = crew_store / f"exec-{os.getpid()}.identity"
    assert identity_file.is_file()

    with pytest.raises(CrewError, match="first-run"):
        _take_claim("second-run")

    assert json.loads(first.path.read_text(encoding="utf-8"))["run_id"] == "first-run"
    assert identity_file.is_file()
    first.release()
    assert not identity_file.exists()


def test_a_re_executed_holder_is_still_reclaimed(crew_store: Path) -> None:
    first = _take_claim("first-run")
    record = json.loads(first.path.read_text(encoding="utf-8"))
    identity_file = crew_store / f"exec-{os.getpid()}.identity"
    assert record["exec_identity"] == identity_file.read_text(encoding="utf-8")

    identity_file.write_text("identity-from-next-exec", encoding="utf-8")
    second = _take_claim("second-run")

    assert second.reclaimed is not None
    assert second.reclaimed["run_id"] == "first-run"
    assert Path(second.reclaimed["moved_to"]).exists()
    second.release()


def _dead_pid() -> int:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    return finished.pid


def test_the_sweep_removes_a_dead_pids_file_and_keeps_a_live_one(
    crew_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    crew_store.mkdir(parents=True, exist_ok=True)
    dead = crew_store / f"exec-{_dead_pid()}.identity"
    dead.write_text("dead-execution", encoding="utf-8")
    live = crew_store / f"exec-{os.getpid()}.identity"
    live.write_text("live-execution", encoding="utf-8")

    report = gc.sweep_exec_identities(apply=True)

    assert [entry["path"] for entry in report["removed"]] == [str(dead)]
    assert report["kept"] == [str(live)]
    assert not dead.exists()
    assert live.is_file()
    assert f"removing identity file {dead}" in capsys.readouterr().err


def test_the_sweep_reports_without_removing_unless_applied(crew_store: Path) -> None:
    crew_store.mkdir(parents=True, exist_ok=True)
    dead = crew_store / f"exec-{_dead_pid()}.identity"
    dead.write_text("dead-execution", encoding="utf-8")

    report = gc.sweep_exec_identities(apply=False)

    assert [entry["path"] for entry in report["removed"]] == [str(dead)]
    assert dead.is_file()


def test_the_sweep_removes_a_file_a_reused_pid_did_not_write(
    crew_store: Path,
) -> None:
    crew_store.mkdir(parents=True, exist_ok=True)
    misattributed = crew_store / f"exec-{os.getpid()}.identity"
    misattributed.write_text("previous-execution", encoding="utf-8")
    os.utime(misattributed, (1, 1))

    swept = gc.sweep_exec_identities(apply=True)

    assert [entry["path"] for entry in swept["removed"]] == [str(misattributed)]
    assert not misattributed.exists()
