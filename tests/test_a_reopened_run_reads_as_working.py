"""A reopened run reads as working, not blocked.

``crew resume --reopen --write-path P`` widens a completed run's claim so the
coordinator can continue the run with a path it did not declare. Healing the
run's delivery by writing a status the reopen chose — ``blocked`` — left the
resumed worker running its whole attempt under a word no attempt had reached:
the run classified as blocked, the obligations read raised a blocked duty for a
run that was working, and coordinators acknowledged it by hand.

The reopen continues the run rather than ruling on it, so the delivered verdict
is cleared the way a resume clears it — leaving the delivered text beside the
manifest under an attempt-named copy — and the reopen's own record is the fact,
its reason, the paths it granted and the copy it kept, never a status word.

Measured 2026-10-21: run ``r-20261021T023811534757-cold-test-discharge-replay``
delivered ``status: complete``, was reopened with
``--reason reopening the completed run``, and its manifest read ``blocked``
while its worker was alive.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import recovery
from reckon.crew import runs as runs_module
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "reopened-fixture"
SESSION = "coordinator-fixture"

# The words the supervisor reads as a delivered verdict.
TERMINAL_WORDS = {"complete", "blocked", "failed"}

REOPEN_RECORD_NAME = "reopened.json"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A temporary, fully environment-resolved crew home and project mount."""
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
        ("commit", "-q", "--allow-empty", "-m", "test: seed reopened fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return {"home": config_home, "root": root}


def _write_a_completed_run(run_id: str, project: dict[str, Path]) -> Path:
    """Write one delivered run: live pointer, manifest and node scope."""
    directory = runs_module.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: complete\n", encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(project["root"]),
        "worktree": str(project["root"]),
        "phase": "complete",
        "launch": "cli",
        "session_id": "fixture-session",
        "manifest_path": str(manifest),
        "node": {"id": run_id, "write_paths": ["seed.txt"]},
    }
    runs_module._write_json(runs_module.pointer_path(run_id), pointer)
    return manifest


def _reopen(run_id: str, monkeypatch: pytest.MonkeyPatch, path: str = "seed.txt"):
    """Run the reopen through the CLI, with the worker launch stubbed out.

    The launch is stubbed the way the fold test stubs it: this node is about
    what the reopen leaves behind, not about starting a worker.
    """
    monkeypatch.setattr("reckon.crew_run_commands._resolved_flight", lambda *args: {})

    def launch(*args, **kwargs):
        plan = type("Plan", (), {"as_dict": lambda self: {}})()
        return {"plan": plan, "pid": 123, "log_path": "fixture.log", "turn": 1}

    monkeypatch.setattr("reckon.crew.resumption._resume", launch)
    return CliRunner().invoke(
        main,
        ["crew", "resume", "--run", run_id, "--reopen", "--write-path", path],
    )


def _manifest_status(manifest: Path) -> str:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"the manifest at {manifest} carries no status line")


def _stub_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runs_module, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 0}
    )


def test_a_reopened_run_leaves_no_verdict_of_the_reopens_own(
    project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is not left reading a word no attempt reached."""
    run_id = "run-reopened-leaves-no-verdict"
    manifest = _write_a_completed_run(run_id, project)

    result = _reopen(run_id, monkeypatch)

    assert result.exit_code == 0, result.output
    status = _manifest_status(manifest)
    assert status not in TERMINAL_WORDS, status
    assert status == "in-progress"
    delivered = sorted(manifest.parent.glob(f"{manifest.name}.attempt-*.asdelivered"))
    assert [path.read_text(encoding="utf-8") for path in delivered] == [
        f"node: {run_id}\nstatus: complete\n"
    ]


def test_a_reopened_run_reads_as_working_and_owes_no_blocked_duty(
    project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run behind the reopened manifest classifies as running."""
    run_id = "run-reopened-reads-working"
    _write_a_completed_run(run_id, project)

    result = _reopen(run_id, monkeypatch)

    assert result.exit_code == 0, result.output
    pointer = runs_module.read_pointer(run_id)
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "running", row

    _stub_drain(monkeypatch)
    owed = obligations_module.obligations(PROJECT, SESSION)
    mine = [item for item in owed["obligations"] if item["run_id"] == run_id]
    assert mine == [], mine
    assert all(item["kind"] != "blocked" for item in owed["obligations"])


def test_a_reopen_records_what_it_granted_and_why(
    project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reopen's own record carries the fact, the reason and the paths."""
    run_id = "run-reopened-records-its-grant"
    _write_a_completed_run(run_id, project)

    result = _reopen(run_id, monkeypatch, path="reckon/crew/claims.py")

    assert result.exit_code == 0, result.output
    record = json.loads(
        (runs_module.run_dir(run_id) / REOPEN_RECORD_NAME).read_text(encoding="utf-8")
    )
    assert record["reason"]
    assert record["granted_paths"] == ["reckon/crew/claims.py"]
    assert record["delivered"]


def test_a_live_worker_reopen_refusal_still_stands(
    project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live worker is refused before anything is granted or cleared."""
    run_id = "run-reopened-refuses-live-worker"
    manifest = _write_a_completed_run(run_id, project)
    pointer = runs_module.read_pointer(run_id)
    pointer["pid"] = os.getpid()
    pointer["process_alive"] = True
    pointer["launcher_host"] = None
    runs_module._write_json(runs_module.pointer_path(run_id), pointer)

    result = _reopen(run_id, monkeypatch)

    assert result.exit_code != 0
    assert _manifest_status(manifest) == "complete"
    assert runs_module.read_pointer(run_id)["node"]["write_paths"] == ["seed.txt"]