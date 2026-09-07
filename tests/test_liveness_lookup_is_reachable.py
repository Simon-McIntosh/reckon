"""The classifier reaches liveness through the module that defines it.

A name imported into another module is bound at import time, so replacing the
attribute on the defining module leaves every such binding untouched. The
measured cost: a test replaced ``process_alive`` on ``reckon.crew.runs`` and
the classifier — holding its own import-time binding — consulted the real
process table instead of the arranged answer, so no failed transition rendered
after the arranged process had stopped.

What this locks in: classification reads ``runs.process_alive`` at the point
of call, so replacing the definition on its owning module replaces what the
classifier consults. Both directions are asserted on one pointer, because a
single direction could be coincidentally right: the replacement reporting
not alive must make a terminal word the outcome, and the same pointer whose
replacement reports alive must defer that word. A pointer with no recorded
launching host is not the classifier's business to ask the process table
about, so it keeps the stored answer and performs no lookup.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

HOST = socket.gethostname()


def _pointer(
    tmp_path: Path, run_id: str, *, launcher_host: str | None, stored_alive: bool | None
) -> dict:
    """One record shaped as a live pointer carrying a terminal failed manifest."""
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\n"
        "status: failed\n"
        "commits: 0123456789abcdef\n"
        "blockers: implementation pending\n"
    )
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "created_at": "2026-09-07T00:00:00Z",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": stored_alive,
        # This test process is genuinely running, so the real table read would
        # answer alive: only a replacement that actually reaches the classifier
        # can make the pair of assertions below hold on one pointer.
        "pid": os.getpid(),
        "launcher_host": launcher_host,
    }


def test_replacing_the_module_function_changes_the_classification(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One pointer, two replacements on the defining module: not alive makes the
    # terminal word the outcome, alive defers it. Because the pid names this
    # genuinely running process, the pair cannot both hold unless the
    # classifier consults the replacement — an import-time binding would read
    # the real process table and answer alive for both.
    pointer = _pointer(tmp_path, "r-reach", launcher_host=HOST, stored_alive=None)

    monkeypatch.setattr(runs, "process_alive", lambda _pid: False)
    dead_row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert dead_row["process_alive"] is False
    assert dead_row["liveness_proven"] is True
    assert dead_row["classification"] == "failed"
    assert dead_row["manifest_status"] == "failed"

    monkeypatch.setattr(runs, "process_alive", lambda _pid: True)
    live_row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert live_row["process_alive"] is True
    assert live_row["liveness_proven"] is True
    assert live_row["classification"] == "running"
    assert live_row["manifest_status"] is None, "a live writer's report is not an outcome"
    assert live_row["manifest_reported_status"] == "failed"


@pytest.mark.parametrize("stored", [None, True, False])
def test_pointer_without_a_launching_host_keeps_the_stored_answer(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stored
) -> None:
    # A pointer with no recorded launching host cannot be shown to belong to
    # this host, so the classifier carries the stored answer and never asks the
    # process table. The lookup is forbidden to run, so a reach that ignores
    # the host gate fails the test by raising.
    def _forbidden_lookup(pid):
        raise AssertionError(
            f"process lookup performed without a recorded host {pid!r}"
        )

    monkeypatch.setattr(runs, "process_alive", _forbidden_lookup)
    row = recovery.classify_pointer(
        _pointer(
            tmp_path,
            f"r-nohost-{stored}",
            launcher_host=None,
            stored_alive=stored,
        ),
        now_seconds=time.time(),
    )
    assert row["process_alive"] is stored
    assert row["liveness_proven"] is False
