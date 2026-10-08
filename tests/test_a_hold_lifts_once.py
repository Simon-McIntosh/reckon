"""A hold is lifted at most once per condition, read from the sweep's own log.

The pointer's ``auto_resume`` slot is only a cache of the lift: a resume's own
launch rewrites the pointer from a snapshot taken before the stamp, so the slot
can read absent for a lift that already happened, and a second sweep of an
unchanged hold lifted the same hold again. The append-only recovery log is the
authority, and a second sweep must find the lift there even after the pointer
was rewritten. Nothing launches: the sweep takes its launcher, so a resume is
observed as the invocation it would spawn.
"""

from __future__ import annotations

import json
import socket
from datetime import timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.resumption import recovery_log_path, sweep
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
REFUSED_STREAM = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)
DEAD_PID = 4_194_303


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _stated_reset():
    """The reset moment the recorded refusal itself names."""
    from reckon.crew.recovery import _stream_refusal_block
    from reckon.crew.resumption import _parse_stamp

    block = _stream_refusal_block(
        {
            "launch": "cli",
            "backend": "alpha",
            "argv": ["codex"],
            "log_path": str(REFUSED_STREAM),
        }
    )
    assert block is not None, "the fixture must be a real recorded refusal"
    moment = _parse_stamp(block["resets_at"])
    assert moment is not None
    return moment


def _refused_run(tmp_path: Path, run_id: str) -> dict:
    """A pointer for a run a provider refusal stopped, as dispatch leaves it."""
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_bytes(REFUSED_STREAM.read_bytes())
    tree = tmp_path / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: node-a\nstatus: blocked\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(tmp_path / "repo"),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "role": "implement",
        "created_at": "2026-09-03T09:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "phase": "working",
        # The end a lift rests on: this host checks the pid and finds it gone.
        "launcher_host": socket.gethostname(),
        "pid": _absent_pid(),
        "node": {
            "id": run_id.rsplit("-", 1)[-1],
            "plan": "plan-a",
            "section": "§7",
            "time_budget": "30m",
            "write_paths": ["reckon/one.py"],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


class _Launcher:
    """Stands in for the spawn, recording what would have been launched."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.calls.append({"log_path": Path(log_path)})
        return DEAD_PID


def _clock_at(monkeypatch: pytest.MonkeyPatch, moment) -> None:
    """Make every clock in the decision read the moment the test states."""
    from reckon import budget as budget_module
    from reckon.crew import resumption as resumption_module

    monkeypatch.setattr(budget_module, "_now", lambda now=None: now or moment)
    monkeypatch.setattr(resumption_module, "_now", lambda now=None: now or moment)


def test_a_pointer_rewrite_cannot_lift_the_same_hold_twice(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log, not the pointer slot, is the authority on a lift."""
    run_id = "r-20260903T090000000000-node-a"
    _refused_run(tmp_path, run_id)
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)
    launcher = _Launcher()

    # A snapshot taken before the sweep's own stamp.
    snapshot = crew.read_pointer(run_id)
    first = sweep(PROJECT, launcher=launcher, now=after)
    assert [item["run_id"] for item in first["resumed"]] == [run_id]
    assert len(launcher.calls) == 1

    # The resume's launch rewrote the pointer from a snapshot taken before the
    # stamp, so the auto_resume slot is gone while the lift happened.
    _write_json(pointer_path(run_id), snapshot)
    assert "auto_resume" not in crew.read_pointer(run_id)

    second = sweep(PROJECT, launcher=launcher, now=after)
    assert second["resumed"] == []
    assert len(launcher.calls) == 1
    assert [item["reason"] for item in second["skipped"]] == [
        "already-resumed-for-this-hold"
    ]
    # And the log records the one lift that happened.
    recorded = [
        json.loads(line)
        for line in recovery_log_path(PROJECT).read_text(encoding="utf-8").splitlines()
    ]
    lifts = [
        entry
        for report in recorded
        if not report.get("dry_run")
        for entry in report.get("resumed") or ()
    ]
    assert [entry["run_id"] for entry in lifts] == [run_id]


def test_a_dry_run_preview_does_not_retire_the_hold(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview appends its own line but lifts nothing to be read back."""
    run_id = "r-20260903T091000000000-node-a"
    _refused_run(tmp_path, run_id)
    after = _stated_reset() + timedelta(minutes=1)
    _clock_at(monkeypatch, after)
    launcher = _Launcher()

    preview = sweep(PROJECT, launcher=launcher, dry_run=True, now=after)
    assert preview["resumed"][0]["would_resume"] is True
    assert launcher.calls == []

    real = sweep(PROJECT, launcher=launcher, now=after)
    assert [item["run_id"] for item in real["resumed"]] == [run_id]
    assert len(launcher.calls) == 1
