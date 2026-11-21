"""A queued message reaches a live run when its launch seeded an inbox it reads.

Whether ``crew tell`` can reach a live run is a question about the run's own
launch, not about the name its harness happens to carry. A harness whose
interface reads a settings group is reached through the inbox hook seeded into
its run home; a codex run is reached through the overrides on its recorded
argv; a harness declaring neither reads its inbox only on a later resume, so a
live run on it is refused.

Every case builds its run directory, crew home and harness home under
``tmp_path``, so nothing reads or writes the operator's own homes or the
workstation's live crew pointers.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from click.testing import CliRunner

from reckon import _worker_fence
from reckon.cli import main
from reckon.hooks import install, worker_inbox

RUN_ID = "r-20261121T070000000000-seeded-inbox"


def _crew_home(tmp_path: Path, record: dict) -> Path:
    home = tmp_path / "crew-home"
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / f"{RUN_ID}.json").write_text(json.dumps(record))
    return home


def _run_dir(tmp_path: Path) -> tuple[Path, Path]:
    """The run directory where the crew home resolves it, as a real run lives."""
    run = tmp_path / "crew-home" / "crew" / "runs" / RUN_ID
    run.mkdir(parents=True, exist_ok=True)
    manifest = run / "manifest.md"
    manifest.write_text("status: in-progress\n")
    return run, manifest


def _record(tmp_path: Path, **overrides) -> dict:
    _, manifest = _run_dir(tmp_path)
    record = {
        "run_id": RUN_ID,
        "dialect": "claude",
        "fenced": True,
        "manifest_path": str(manifest),
        "launcher_host": socket.gethostname(),
        "pid": os.getpid(),
        "worktree": str(tmp_path / "worktree"),
    }
    record.update(overrides)
    return record


def _tell(message: str = "steer left"):
    return CliRunner().invoke(
        main, ["crew", "tell", "--run", RUN_ID, message], catch_exceptions=False
    )


def _seed_run_home(tmp_path: Path, dialect: str) -> Path:
    """Seed a run home the way the launch does, hook included.

    The settings file is passed exactly as the launch passes it, because that
    argument is what makes the seeder register the inbox hook at all: a home
    seeded without it carries no hook, and a test that omitted it would be
    exercising a launch no worker has ever had.
    """
    run, _ = _run_dir(tmp_path)
    operator = tmp_path / "operator"
    (operator / ".claude").mkdir(parents=True)
    run_home = run / "harness"
    _worker_fence.seed_harness_home(
        run_home,
        dialect_name=dialect,
        operator_home=operator,
        declaration=[{"path": "settings.json", "keys": ["hooks"]}],
        settings_file="settings.json",
    )
    return run_home


def test_a_live_run_on_the_excluded_harness_queues_its_message(
    tmp_path: Path, monkeypatch
):
    """The clive dialect is seeded a claude-shaped home and its message queues.

    ``crew tell`` once admitted only the dialect literally named ``claude``, so
    a live run recorded under this harness was refused although its launch had
    seeded the very hook that reads its inbox.
    """
    run, manifest = _run_dir(tmp_path)
    _seed_run_home(tmp_path, "clive")
    record = _record(tmp_path, dialect="clive", manifest_path=str(manifest))
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "queued_for_next_tool_call"
    assert payload["inbox"] == str(run / "inbox")
    queued = worker_inbox.pending_messages(run)
    assert len(queued) == 1
    assert queued[0].read_text() == "steer left"


def test_a_live_run_whose_launch_seeded_no_inbox_is_refused_naming_it(
    tmp_path: Path, monkeypatch
):
    """The admitted harness with no seeded home is refused, naming the inbox."""
    run, manifest = _run_dir(tmp_path)
    record = _record(tmp_path, manifest_path=str(manifest))
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "carries no PostToolUse entry" in result.output
    assert str(run / "inbox") in result.output
    assert "queued_for_next_tool_call" not in result.output
    assert worker_inbox.pending_messages(run) == []


def test_a_run_that_has_ended_is_refused_as_it_is_today(tmp_path: Path, monkeypatch):
    """A stopped run is refused on its end, before any inbox question is asked."""
    run, manifest = _run_dir(tmp_path)
    _seed_run_home(tmp_path, "clive")
    record = _record(
        tmp_path,
        dialect="clive",
        manifest_path=str(manifest),
        launcher_host="some-other-node",
        process_alive=False,
    )
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "has ended" in result.output
    assert "crew resume" in result.output
    assert worker_inbox.pending_messages(run) == []


def test_the_hook_the_seeder_writes_is_the_one_the_writer_looks_for(tmp_path: Path):
    """Positive control: the seeder's artifact is what the reachability test reads."""
    run_home = _seed_run_home(tmp_path, "clive")
    assert install.settings_carry_worker_inbox(run_home / "settings.json")
    assert install.WORKER_INBOX_SCRIPT_NAME in (run_home / "settings.json").read_text()
