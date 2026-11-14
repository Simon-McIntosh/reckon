"""An overdue review duty's figure moves with the clock between snapshots.

A published obligations snapshot stores each duty's ages as the instant they are
measured from, so a reader recomputes ``age_seconds`` against its own read
instant and never shows a frozen age. The duty's remedy text is prose composed
at publish time, and the only reader-side refresh once covered the numeric ages:
the "review overdue by" figure in that text stayed at the value it had when the
snapshot was written, while ``age_seconds`` beside it kept growing.

The case publishes one delivered run's duty through the station's own sweep at
a fixed instant, then reads the stored snapshot twice, 120 s apart, through
``live_payload`` -- the shape a pane is handed. The overdue figure the reason
prints must equal the row's refreshed ``age_seconds`` at each reading, and the
two readings must differ by exactly the 120 s between them. At the base revision the second reading repeats the first figure, because
the prose is served from the snapshot unchanged.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import follower_registration, obligation_snapshot, runs
from tests.helpers.git import git as _git

ledger_module = importlib.import_module("reckon.ledger")
obligations_module = importlib.import_module("reckon.crew.obligations")
recovery_classification = importlib.import_module("reckon.crew.recovery_classification")

PROJECT = "overdue-figure-clock"
SESSION = "coordinator-overdue-figure-clock"
RUN_ID = "run-overdue-figure-clock"
# The run delivers well past the reflex's sweep window, so its duty is already
# overdue at the first reading and the only thing that can move the figure is
# the reader's clock.
DELIVERED_AGE_SECONDS = 2000
READING_GAP_SECONDS = 120
T0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

_OVERDUE = re.compile(r"review overdue by (\d+)m (\d+)s")


@pytest.fixture()
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Publish one overdue duty's snapshot and hand back the stored document."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the overdue-figure fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = ledger_module.ledger_path(PROJECT).parent
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    ledger_module.ledger_path(PROJECT).write_text(
        json.dumps({"data": {"_version": 1, "members": [], "runs": [], "holds": []}}),
        encoding="utf-8",
    )

    reviewed_head = _git(root, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    _git(root, "worktree", "add", "-q", "--detach", str(worktree), reviewed_head)
    (worktree / "reckon").mkdir()
    (worktree / "reckon" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(worktree, "add", "reckon/mod.py")
    _git(worktree, "commit", "-q", "-m", "test: change one module")
    head = _git(worktree, "rev-parse", "HEAD")

    manifest = config_home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {RUN_ID}\nstatus: complete\ncommits: [{head}]\n"
        "changed_paths: [reckon/mod.py]\n",
        encoding="utf-8",
    )
    # The classifier ages a delivered run from its manifest's mtime.
    aged = (T0 - timedelta(seconds=DELIVERED_AGE_SECONDS)).timestamp()
    os.utime(manifest, (aged, aged))
    pointer = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(root),
        "worktree": str(worktree),
        "base_sha": reviewed_head,
        "process_alive": False,
        "phase": "complete",
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "launcher_host": os.uname().nodename,
        "node": {
            "id": RUN_ID,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": ["reckon/mod.py"],
        },
    }
    runs._write_json(runs.pointer_path(RUN_ID), pointer)

    monkeypatch.setattr(obligations_module, "_utc_now", lambda: T0)
    monkeypatch.setattr(recovery_classification, "_utc_seconds", T0.timestamp)
    producer = {
        "pid": os.getpid(),
        "pid_start_time": None,
        "started_at": T0.isoformat(),
        "code_stamp": "overdue-figure-clock",
    }
    with follower_registration.follower_registration(
        PROJECT, SESSION, delivery="stream"
    ) as registration:
        assert registration.held
        # No stored review and an armed follower: the duty reads review-missing
        # and the reflex owns the dispatch, which is the branch that prints the
        # overdue figure.
        written = obligation_snapshot.sweep(
            PROJECT,
            sessions=[SESSION],
            producer=producer,
            stream_offset=0,
            now=T0,
        )
    assert written, "the first sweep of a producer publishes"
    document = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert document is not None
    return {"document": document}


def _overdue_row(payload: dict[str, Any]) -> dict[str, Any]:
    rows = [
        row
        for row in payload.get("obligations") or ()
        if row.get("kind") == "review-missing"
    ]
    assert len(rows) == 1, payload.get("obligations")
    return rows[0]


def _overdue_seconds(row: dict[str, Any]) -> int:
    text = f"{row.get('reason') or ''} {row.get('next_command') or ''}"
    match = _OVERDUE.search(text)
    assert match is not None, text
    return int(match.group(1)) * 60 + int(match.group(2))


def test_an_overdue_figure_tracks_the_clock(published: dict[str, Any]) -> None:
    """Two readings 120 s apart print figures 120 s apart, beside their ages."""
    document = published["document"]
    first = _overdue_row(obligation_snapshot.live_payload(document, now=T0))
    second = _overdue_row(
        obligation_snapshot.live_payload(
            document, now=T0 + timedelta(seconds=READING_GAP_SECONDS)
        )
    )

    window = int(first["reflex_window_seconds"])
    assert window > 0
    assert first["age_seconds"] > window, "the duty is past the reflex's window"
    assert second["age_seconds"] - first["age_seconds"] == READING_GAP_SECONDS

    # The figure must move with the age beside it: two readings a fixed gap
    # apart cannot print the same overdue figure.
    assert _overdue_seconds(second) - _overdue_seconds(first) == READING_GAP_SECONDS
    assert _overdue_seconds(first) == first["age_seconds"]
    assert _overdue_seconds(second) == second["age_seconds"]
