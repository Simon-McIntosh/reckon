"""`crew tell` reaches whichever harness a live run was actually launched with.

A coordinator's message reaches a worker only if something the worker's own
launch put in place reads the run directory's inbox. That artifact differs by
dialect: a Claude-dialect run carries the inbox hook in its own run home's
settings file, while a codex run carries the hook and the trust record for it as
``-c`` overrides on its launch argv. Each case here builds its run directory,
its crew home and its harness homes under ``tmp_path``, so nothing reads or
writes the operator's own homes or the workstation's live crew pointers.

The codex overrides are assembled in the shape the codex dialect composes them,
which is what a real run record carries: the hook definition first and the trust
record for exactly that hook beside it. A run launched before those overrides
existed is a record whose argv holds neither, and it is refused for that reason
rather than for its dialect.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _worker_fence
from reckon.cli import main
from reckon.hooks import install, worker_inbox

RUN_ID = "r-20261111T120000000000-tell-codex"


def _run_dir(tmp_path: Path) -> tuple[Path, Path]:
    run = tmp_path / "runs" / RUN_ID
    run.mkdir(parents=True, exist_ok=True)
    manifest = run / "manifest.md"
    manifest.write_text("status: in-progress\n")
    return run, manifest


def _crew_home(tmp_path: Path, record: dict) -> Path:
    home = tmp_path / "crew-home"
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / f"{RUN_ID}.json").write_text(json.dumps(record))
    return home


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


def _seeded_claude_home(tmp_path: Path) -> Path:
    """A Claude-dialect run home seeded through the fence's own seeder."""
    run, _ = _run_dir(tmp_path)
    operator = tmp_path / "operator"
    (operator / ".claude").mkdir(parents=True)
    run_home = run / "harness"
    _worker_fence.seed_harness_home(
        run_home,
        dialect_name="claude",
        operator_home=operator,
        declaration=[{"path": "settings.json", "keys": ["hooks"]}],
    )
    return run_home


def _codex_argv(*, hook: bool = True, trust: bool = True, worktree: str = "worktree"):
    """A recorded codex launch argv carrying the overrides the dialect composes."""
    words = ["/usr/bin/codex", "exec", "--json", "-C", worktree]
    if hook:
        command = install.worker_inbox_group()["hooks"][0]["command"]
        words += [
            "-c",
            'hooks.PostToolUse=[{hooks=[{type="command", command='
            + json.dumps(command)
            + "}]}]",
        ]
    if trust:
        words += [
            "-c",
            'hooks.state={"/<session-flags>/config.toml:post_tool_use:0:0"'
            '={trusted_hash="sha256:' + "a" * 64 + '"}}',
        ]
    return words


def _without_argv(record: dict) -> dict:
    record.pop("argv", None)
    return record


# ── codex runs: reachability is the recorded launch argv ─────────────────────


def test_crew_tell_writes_to_a_live_codex_run_carrying_both_overrides(
    tmp_path: Path, monkeypatch
):
    run, manifest = _run_dir(tmp_path)
    record = _record(
        tmp_path,
        dialect="codex",
        manifest_path=str(manifest),
        argv=_codex_argv(worktree=str(tmp_path)),
    )
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "queued_for_next_tool_call"
    assert payload["run_id"] == RUN_ID
    queued = worker_inbox.pending_messages(run)
    assert len(queued) == 1
    assert queued[0].read_text() == "steer left"


def test_crew_tell_refuses_a_codex_run_whose_argv_lacks_the_trust_override(
    tmp_path: Path, monkeypatch
):
    run, manifest = _run_dir(tmp_path)
    record = _record(
        tmp_path,
        dialect="codex",
        manifest_path=str(manifest),
        argv=_codex_argv(trust=False),
    )
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "no trust override" in result.output
    assert "queued_for_next_tool_call" not in result.output
    assert worker_inbox.pending_messages(run) == []


def test_crew_tell_refuses_a_codex_run_whose_argv_lacks_the_hook_override(
    tmp_path: Path, monkeypatch
):
    run, manifest = _run_dir(tmp_path)
    record = _record(
        tmp_path,
        dialect="codex",
        manifest_path=str(manifest),
        argv=_codex_argv(hook=False),
    )
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "no inbox hook override" in result.output
    assert worker_inbox.pending_messages(run) == []


@pytest.mark.parametrize(
    "argv",
    [
        # Launched before the overrides existed: the plain composition.
        ["/usr/bin/codex", "exec", "--json", "-C", "worktree", "-"],
        # A different -c override for an unrelated setting.
        ["/usr/bin/codex", "exec", "--json", "-c", "model=gpt-5", "-"],
        # No argv recorded at all.
        None,
    ],
)
def test_crew_tell_refuses_a_codex_run_launched_before_the_overrides_existed(
    tmp_path: Path, monkeypatch, argv
):
    run, manifest = _run_dir(tmp_path)
    record = _without_argv(
        _record(tmp_path, dialect="codex", manifest_path=str(manifest))
    )
    if argv is not None:
        record["argv"] = argv
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "neither the inbox hook override nor its trust override" in result.output
    assert "queued_for_next_tool_call" not in result.output
    assert worker_inbox.pending_messages(run) == []


# ── other dialects are refused, naming why ───────────────────────────────────


@pytest.mark.parametrize("dialect", ["", "omp", "clive"])
def test_crew_tell_refuses_a_dialect_it_cannot_reach(
    tmp_path: Path, monkeypatch, dialect
):
    run, manifest = _run_dir(tmp_path)
    record = _record(tmp_path, dialect=dialect, manifest_path=str(manifest))
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert f"{dialect or 'unknown'}-dialect run" in result.output
    assert "reaches only Claude-dialect runs" in result.output
    assert worker_inbox.pending_messages(run) == []


# ── the Claude-dialect cases are decided by the run home, unchanged ──────────


def test_crew_tell_still_writes_to_a_live_fenced_claude_run(
    tmp_path: Path, monkeypatch
):
    run, manifest = _run_dir(tmp_path)
    _seeded_claude_home(tmp_path)
    record = _record(tmp_path, manifest_path=str(manifest))
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "queued_for_next_tool_call"
    assert len(worker_inbox.pending_messages(run)) == 1


def test_crew_tell_still_refuses_a_claude_run_whose_home_lacks_the_hook(
    tmp_path: Path, monkeypatch
):
    run, manifest = _run_dir(tmp_path)
    run_home = _seeded_claude_home(tmp_path)
    settings = json.loads((run_home / "settings.json").read_text())
    settings["hooks"].pop("PostToolUse", None)
    (run_home / "settings.json").write_text(json.dumps(settings))
    record = _record(tmp_path, manifest_path=str(manifest))
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "carries no PostToolUse entry" in result.output
    assert worker_inbox.pending_messages(run) == []


def test_crew_tell_still_refuses_an_unfenced_claude_run(tmp_path: Path, monkeypatch):
    run, manifest = _run_dir(tmp_path)
    record = _record(tmp_path, manifest_path=str(manifest), fenced=False)
    monkeypatch.setenv("RECKON_HOME", str(_crew_home(tmp_path, record)))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "not fenced" in result.output
    assert worker_inbox.pending_messages(run) == []


def test_the_codex_predicate_reads_the_overrides_the_dialect_composes():
    """The predicate accepts exactly the two overrides production composes."""
    assert install.worker_inbox_group()["hooks"][0]["command"]
    assert install.WORKER_INBOX_SCRIPT_NAME
    from reckon.crew_run_commands import _codex_inbox_override_gap

    assert _codex_inbox_override_gap(_codex_argv()) is None
    assert _codex_inbox_override_gap(_codex_argv(trust=False)) == "no trust override"
