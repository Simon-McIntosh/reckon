"""A live writer's terminal manifest word is a report, not an outcome.

The measured case: a worker wrote an orientation manifest forty seconds after
dispatch carrying ``status: failed``, an empty commit list, and a blockers
entry reading "pending implementation"; the follower classified the run failed
while the worker was still alive and working for a further eighteen minutes.
Liveness was available and was not consulted.

What this locks in: while the process that writes a manifest is alive, a
terminal word in it (``failed``, ``blocked``, ``complete``) is work still in
progress, not the run's verdict. The classification arms defer to the same rule
the stored-phase arm already applies — an alive process outranks a terminal
report. A pid whose liveness cannot be proven defers nothing (an unprovable pid
is not proof of death, and equally not proof of life), so an unprovable run
classifies exactly as before. Once the process is proven dead, each terminal
word regains its existing meaning, and a manifest rewritten after exit is
classified from its final content so the rewritten-report transition still has
its signal.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from reckon.crew import recovery


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    status: str | None,
    alive: bool | None,
    phase: str = "working",
) -> dict:
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    if status:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        # The blockers phrase mirrors the measured orientation manifest; it is
        # delivered so the terminal arms have prose to explain themselves, not
        # because any arm matches on it.
        manifest.write_text(
            f"node: {run_id}\n"
            f"status: {status}\n"
            "commits: 0123456789abcdef\n"
            "blockers: implementation pending\n"
        )
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": "2026-09-07T00:00:00Z",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": alive,
    }


def test_an_alive_process_failed_word_is_a_report_not_an_outcome(tmp_path) -> None:
    # The measured arbitration: a failed word from a process that is alive is a
    # report in progress, so the run stays running and the effective status is
    # not exposed as an outcome. The raw spelling is retained for the one-shot
    # watcher that must recognise a fresh completion from the resumed attempt.
    row = recovery.classify_pointer(
        _pointer(tmp_path, "r-live-failed", status="failed", alive=True),
        now_seconds=time.time(),
    )
    assert row["classification"] == "running"
    assert row["process_alive"] is True
    assert row["manifest_status"] is None, "a live writer's report is not an outcome"
    assert row["manifest_reported_status"] == "failed"
    assert row["detail"] == "the process is alive"


def test_a_dead_process_failed_word_classifies_exactly_as_today(tmp_path) -> None:
    # Positive proof of death restores the word's meaning: a failed report from
    # a gone process is the run's outcome, unchanged by the live deferral.
    row = recovery.classify_pointer(
        _pointer(tmp_path, "r-dead-failed", status="failed", alive=False),
        now_seconds=time.time(),
    )
    assert row["classification"] == "failed"
    assert row["manifest_status"] == "failed"
    assert "implementation pending" in row["detail"]
    assert row["terminal_at"] is not None
    assert row["terminal_age_seconds"] is not None


def test_an_unprovable_pid_leaves_the_failed_word_classification_unchanged(
    tmp_path,
) -> None:
    # An unprovable pid is not proof of death, and it is not proof of life
    # either: the deferral asks for liveness, so an unknown process defers
    # nothing and the manifest word classifies as it always has.
    row = recovery.classify_pointer(
        _pointer(tmp_path, "r-unknown-failed", status="failed", alive=None),
        now_seconds=time.time(),
    )
    assert row["classification"] == "failed"
    assert row["manifest_status"] == "failed"
    assert row["process_alive"] is None


@pytest.mark.parametrize(
    ("status", "dead_classification"),
    [
        ("blocked", "blocked"),
        ("complete", "completed_unpromoted"),
    ],
)
def test_every_terminal_word_defers_under_a_live_writer(
    tmp_path, status, dead_classification
) -> None:
    # The deferral covers every terminal word, not only the measured failed
    # one; each keeps its existing meaning once the writer is proven dead.
    alive_row = recovery.classify_pointer(
        _pointer(tmp_path, f"r-live-{status}", status=status, alive=True),
        now_seconds=time.time(),
    )
    assert alive_row["classification"] == "running"
    assert alive_row["manifest_status"] is None

    dead_row = recovery.classify_pointer(
        _pointer(tmp_path, f"r-dead-{status}", status=status, alive=False),
        now_seconds=time.time(),
    )
    assert dead_row["classification"] == dead_classification
    assert dead_row["manifest_status"] == status

    unknown_row = recovery.classify_pointer(
        _pointer(tmp_path, f"r-unknown-{status}", status=status, alive=None),
        now_seconds=time.time(),
    )
    assert unknown_row["classification"] == dead_classification


def test_a_rewrite_after_exit_classifies_from_the_final_content(tmp_path) -> None:
    # A worker that replaces its terminal report after dying (the measured
    # empty-commit failed placeholder overwritten by the real report) is read
    # from the final content, and the digest change is surfaced so the
    # rewritten-report transition still fires on the fold.
    record = _pointer(tmp_path, "r-rewrite", status="failed", alive=False)
    manifest = Path(record["manifest_path"])

    first = recovery.classify_pointer(record, now_seconds=time.time())
    assert first["classification"] == "failed"
    assert first["manifest_commits"] == ["0123456789abcdef"]

    manifest.write_text(
        '{"status": "failed", "commits": ["aaaaaaaa11111111", "bbbbbbbb22222222"]}'
    )
    second = recovery.classify_pointer(record, now_seconds=time.time())
    assert second["classification"] == "failed"
    assert second["manifest_commits"] == ["aaaaaaaa11111111", "bbbbbbbb22222222"]
    assert second["manifest_digest"] != first["manifest_digest"], (
        "the digested content changed, so the watcher can emit its own transition"
    )


def test_the_terminal_timestamp_stays_attached_to_a_dead_process(tmp_path) -> None:
    # The deferral that hides the outcome from a live writer must not hide the
    # record of when a dead run went terminal: a proven-dead process still gets
    # its terminal timestamp, a live one defers both the verdict and the clock.
    dead = recovery.classify_pointer(
        _pointer(tmp_path, "r-dead-ts", status="blocked", alive=False),
        now_seconds=time.time(),
    )
    assert dead["classification"] == "blocked"
    assert dead["terminal_at"] is not None
    assert dead["terminal_at"].endswith("Z")
    assert isinstance(dead["terminal_age_seconds"], int)

    live = recovery.classify_pointer(
        _pointer(tmp_path, "r-live-ts", status="blocked", alive=True),
        now_seconds=time.time(),
    )
    assert live["classification"] == "running"
    assert live["terminal_at"] is None
    assert live["terminal_age_seconds"] is None
