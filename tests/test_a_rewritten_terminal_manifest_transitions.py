"""A worker that replaces its own terminal report wakes the watcher.

A worker writes an orientation manifest early and its real manifest later, and
both may carry the same status word — the measured case is an empty-commit
``failed`` placeholder overwritten eighteen minutes later by the real
``failed`` manifest carrying two commits. The classification does not move
between the two reads, so the fold emits no state change and a coordinator
holds the first verdict forever. These tests bind the rewrite detection: a
terminal manifest whose content changes emits a transition even when its
status word does not, while a manifest that is only touched, or never changed
at all, stays silent.

Detection is a content digest rather than the manifest mtime, asserted
directly by the identical-content falsifier: a rewrite of byte-identical
content changes the mtime and must still emit nothing, so a per-touch
emission is caught.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import recovery

PROJECT = "proj"


def _run_directory(tmp_path: Path, run_id: str) -> Path:
    directory = tmp_path / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _manifest(run_directory: Path) -> Path:
    return run_directory / "manifest.md"


def _write(
    run_directory: Path, *, status: str, commits: list[str], blockers: list[str]
) -> Path:
    """Write one terminal manifest and return its path."""
    manifest = _manifest(run_directory)
    manifest.write_text(
        json.dumps(
            {"status": status, "commits": commits, "blockers": blockers},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest


def _pointer(tmp_path: Path, run_id: str, manifest: Path) -> dict:
    """A dead run carrying a delivered manifest: the post-rewrite reading.

    ``process_alive`` falsifies the deferral of a live process so the terminal
    report is an effective verdict, mirroring the follower read in the measured
    case. The run is left without a pid so ``list_live`` never re-derives
    liveness from the process table.
    """
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": f"session-{run_id}",
        "role": "implement",
        "agent": {"backend": "codex", "model": "gpt-6", "effort": "high"},
        "node": {"id": run_id, "plan": "plan-a", "section": "s8", "role": "implement"},
        "worktree": str(tmp_path / "trees" / run_id),
        "log_path": str(_run_directory(tmp_path, run_id) / "stream.jsonl"),
        "stderr_path": str(_run_directory(tmp_path, run_id) / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "working",
        "process_alive": False,
    }


def _snapshot(tmp_path: Path, run_id: str, manifest: Path, *, moment: float) -> dict:
    return recovery._watch_snapshot(
        _pointer(tmp_path, run_id, manifest), moment=moment, stall_seconds=3600
    )


def _emitted(
    snapshot: dict,
    previous: str | None,
    state: str,
    counts: dict,
) -> dict:
    """Build the persistent transition object the way ``watch_ticker`` does."""
    return recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous=previous,
        current=state,
        counts=counts,
    )


def test_a_rewritten_terminal_manifest_emits_a_transition_carrying_new_commits(
    tmp_path: Path,
) -> None:
    """An empty-commit failed placeholder replaced by the real failed report wakes the reader.

    The status word never moves; only the report beneath it does. The emitted
    transition carries the commit count from the new content, so the
    coordinator learns the actual outcome rather than the placeholder.
    """
    directory = _run_directory(tmp_path, "r-orientation")
    orientation = _write(
        directory, status="failed", commits=[], blockers=["pending implementation"]
    )
    moment = 1_800_000.0
    known = {
        "r-orientation": _snapshot(
            tmp_path, "r-orientation", orientation, moment=moment
        )
    }

    real = _write(
        directory,
        status="failed",
        commits=["abc1234", "def5678"],
        blockers=["gate unmet"],
    )
    current = {
        "r-orientation": _snapshot(
            tmp_path, "r-orientation", real, moment=moment + 18 * 60
        )
    }

    events, next_known = recovery.fleet_transitions(known, current)

    assert len(events) == 1, "the rewrite must emit exactly one transition"
    snapshot, previous, state, counts = events[0]
    assert (previous, state) == ("failed", "failed")
    emitted = _emitted(snapshot, previous, state, counts)
    assert emitted["event"] == "manifest-rewritten"
    assert emitted["commit_count"] == 2
    assert emitted["manifest_commits"] == ["abc1234", "def5678"]
    assert emitted["manifest_status"] == "failed"
    assert next_known["r-orientation"]["state"] == "failed"


def test_a_rewrite_to_identical_content_emits_no_transition(tmp_path: Path) -> None:
    """A touch of the terminal report is not news.

    The mtime moves on every write; a digest does not when the bytes did not.
    Requiring the digest to change is what tells a genuine rewrite from a
    re-write of the same report.
    """
    directory = _run_directory(tmp_path, "r-touch")
    manifest = _write(
        directory, status="failed", commits=["abc1234"], blockers=["gate unmet"]
    )
    moment = 1_800_000.0
    known = {"r-touch": _snapshot(tmp_path, "r-touch", manifest, moment=moment)}

    manifest.write_text(
        json.dumps(
            {"status": "failed", "commits": ["abc1234"], "blockers": ["gate unmet"]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    current = {"r-touch": _snapshot(tmp_path, "r-touch", manifest, moment=moment + 1)}

    events, _ = recovery.fleet_transitions(known, current)
    assert events == []


def test_an_unchanged_manifest_emits_nothing_over_three_reads(tmp_path: Path) -> None:
    """Repeated reads of the same report must not each emit news.

    A per-read emission would wake the coordinator once per sweep for a run
    nothing has changed about.
    """
    directory = _run_directory(tmp_path, "r-stable")
    manifest = _write(
        directory, status="failed", commits=["abc1234"], blockers=["gate unmet"]
    )
    moment = 1_800_000.0
    read_one = _snapshot(tmp_path, "r-stable", manifest, moment=moment)

    # Read two and three are separate reads of the same unchanged file.
    for index in (2, 3):
        following = _snapshot(tmp_path, "r-stable", manifest, moment=moment + index)
        events, _ = recovery.fleet_transitions(
            {"r-stable": read_one}, {"r-stable": following}
        )
        assert events == []
        read_one = following


def test_a_rewrite_that_changes_the_status_word_emits_exactly_one_transition(
    tmp_path: Path,
) -> None:
    """A rewrite that also moves the verdict is a state change, not a rewrite too.

    The status flips from failed to complete, so the fold emits the one state
    change those facts represent; stacking a second, rewrite-shaped event on
    top would report the same news twice.
    """
    directory = _run_directory(tmp_path, "r-status-flip")
    failed = _write(directory, status="failed", commits=[], blockers=["gate unmet"])
    moment = 1_800_000.0
    known = {
        "r-status-flip": _snapshot(tmp_path, "r-status-flip", failed, moment=moment)
    }

    completed = _write(directory, status="complete", commits=["abc1234"], blockers=[])
    current = {
        "r-status-flip": _snapshot(
            tmp_path, "r-status-flip", completed, moment=moment + 1
        )
    }

    events, _ = recovery.fleet_transitions(known, current)

    assert len(events) == 1
    snapshot, previous, state, counts = events[0]
    assert (previous, state) == ("failed", "complete")
    emitted = _emitted(snapshot, previous, state, counts)
    assert emitted["event"] == "transition"
    # The state change carries no rewrite-only facts: the report's content is
    # news here because the verdict moved, and the status flip is that news.
    assert "commit_count" not in emitted


def test_the_rewrite_transition_is_distinguishable_in_its_own_fields(
    tmp_path: Path,
) -> None:
    """A rewrite and a state change read differently on the record itself.

    The distinction is carried by the persistent event fields, not by the
    rendered line, so a reader of the stream can tell the classification word
    did not move from the case where it did without parsing prose.
    """
    rewrite_dir = _run_directory(tmp_path, "r-distinct-rewrite")
    change_dir = _run_directory(tmp_path, "r-distinct-change")
    moment = 1_800_000.0

    # A rewrite: failed with no commits replaced by failed with commits.
    placeholder = _write(
        rewrite_dir, status="failed", commits=[], blockers=["placeholder"]
    )
    rewrite_known = {
        "r-distinct-rewrite": _snapshot(
            tmp_path, "r-distinct-rewrite", placeholder, moment=moment
        )
    }
    full = _write(
        rewrite_dir, status="failed", commits=["abc1234"], blockers=["gate unmet"]
    )
    (rewrite_events, _) = recovery.fleet_transitions(
        rewrite_known,
        {
            "r-distinct-rewrite": _snapshot(
                tmp_path, "r-distinct-rewrite", full, moment=moment + 1
            )
        },
    )
    (rewrite_snapshot, rewrite_previous, rewrite_state, rewrite_counts) = (
        rewrite_events[0]
    )
    rewrite = _emitted(
        rewrite_snapshot, rewrite_previous, rewrite_state, rewrite_counts
    )

    # A state change: failed replaced by complete.
    before = _write(change_dir, status="failed", commits=[], blockers=["gate unmet"])
    change_known = {
        "r-distinct-change": _snapshot(
            tmp_path, "r-distinct-change", before, moment=moment
        )
    }
    after = _write(change_dir, status="complete", commits=["abc1234"], blockers=[])
    (change_events, _) = recovery.fleet_transitions(
        change_known,
        {
            "r-distinct-change": _snapshot(
                tmp_path, "r-distinct-change", after, moment=moment + 1
            )
        },
    )
    (change_snapshot, change_previous, change_state, change_counts) = change_events[0]
    changed = _emitted(change_snapshot, change_previous, change_state, change_counts)

    assert rewrite["event"] == "manifest-rewritten"
    assert rewrite["from_state"] == rewrite["to_state"] == "failed"
    assert changed["event"] == "transition"
    assert changed["from_state"] != changed["to_state"]
    assert rewrite["from_state"] == changed["from_state"]


def test_a_run_with_no_manifest_emits_nothing_and_does_not_raise(
    tmp_path: Path,
) -> None:
    """A run that never delivered a report produces no rewrite news.

    Nothing to compare, nothing to emit, and no exception from the missing
    file: absence is a fact of the run, not an error for the fold.
    """
    directory = _run_directory(tmp_path, "r-no-manifest")
    missing = directory / "manifest.md"
    moment = 1_800_000.0
    known = {
        "r-no-manifest": _snapshot(tmp_path, "r-no-manifest", missing, moment=moment)
    }
    current = {
        "r-no-manifest": _snapshot(
            tmp_path, "r-no-manifest", missing, moment=moment + 1
        )
    }

    events, _ = recovery.fleet_transitions(known, current)
    assert events == []
    # The run reads as abandoned, which is the existing no-manifest verdict.
    assert known["r-no-manifest"]["state"] == "abandoned"


def test_an_inprogress_manifest_update_stays_silent(tmp_path: Path) -> None:
    """Progress on a report that has not reached a verdict is not news.

    A live worker updating its in-progress manifest, like a dead run whose
    report never reached a terminal status, must not fire — those writes are
    progress, and a transition without a verdict is the noise this display
    exists to avoid.
    """
    directory = _run_directory(tmp_path, "r-progress")
    manifest = _write(directory, status="waiting", commits=[], blockers=[])
    moment = 1_800_000.0
    known = {"r-progress": _snapshot(tmp_path, "r-progress", manifest, moment=moment)}

    manifest.write_text(
        json.dumps(
            {"status": "waiting", "commits": ["abc1234"], "blockers": []},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    current = {
        "r-progress": _snapshot(tmp_path, "r-progress", manifest, moment=moment + 1)
    }

    events, _ = recovery.fleet_transitions(known, current)
    assert events == []


def test_the_rewrite_marker_does_not_leak_into_a_later_departure(
    tmp_path: Path,
) -> None:
    """The rewrite is one transition; the run's later promotion stays a promotion.

    The marker that distinguishes the rewrite is carried only on the emitted
    record, never on the run's remembered snapshot, so a departure after the
    rewrite still reads as promoted and not as a second rewrite.
    """
    directory = _run_directory(tmp_path, "r-then-promoted")
    manifest = _write(directory, status="failed", commits=[], blockers=["placeholder"])
    moment = 1_800_000.0
    known = {
        "r-then-promoted": _snapshot(
            tmp_path, "r-then-promoted", manifest, moment=moment
        )
    }
    full = _write(
        directory, status="failed", commits=["abc1234"], blockers=["gate unmet"]
    )
    rewritten, next_known = recovery.fleet_transitions(
        known,
        {
            "r-then-promoted": _snapshot(
                tmp_path, "r-then-promoted", full, moment=moment + 1
            )
        },
    )
    assert len(rewritten) == 1

    departed, _ = recovery.fleet_transitions(next_known, {})
    assert len(departed) == 1
    (snapshot, previous, state, counts) = departed[0]
    emitted = _emitted(snapshot, previous, state, counts)
    assert (previous, state) == ("failed", "promoted")
    assert emitted["event"] == "transition"
    assert "manifest_rewritten" not in snapshot


def test_a_live_processs_terminal_report_rewrite_stays_deferred_too(
    tmp_path: Path,
) -> None:
    """Deferral is unchanged: a live worker's terminal report is not a verdict.

    The classifier already keeps a live process running despite a terminal
    report, so its rewrite must be just as silent — surfacing it would
    contradict the deferral by calling a non-verdict news.
    """
    directory = _run_directory(tmp_path, "r-live-deferred")
    manifest = _write(directory, status="failed", commits=[], blockers=["placeholder"])
    moment = 1_800_000.0
    record = _pointer(tmp_path, "r-live-deferred", manifest)
    record["phase"] = "working"
    record["process_alive"] = True
    known = {
        "r-live-deferred": recovery._watch_snapshot(
            record, moment=moment, stall_seconds=3600
        )
    }
    assert known["r-live-deferred"]["state"] == "working"

    _write(directory, status="failed", commits=["abc1234"], blockers=["gate unmet"])
    current = {
        "r-live-deferred": recovery._watch_snapshot(
            record, moment=moment + 900, stall_seconds=3600
        )
    }

    events, _ = recovery.fleet_transitions(known, current)
    assert events == []
