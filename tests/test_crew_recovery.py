"""Hermetic recovery and project-watch stream contracts."""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import _backends, crew
from reckon import cli as cli_module
from reckon.crew import recovery, reports, runs
from reckon.crew import ticker as ticker_module

# Recorded worker event streams, read as repository fixtures so a refusal is
# asserted against what a real harness wrote rather than against text a test
# constructs. See the README beside them for provenance and elisions.
BACKEND_FIXTURES = Path(__file__).parent / "fixtures" / "backends"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move every crew pointer and watcher claim into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(home: Path, run_id: str, *, terminal: bool) -> None:
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{run_id}.md"
    if terminal:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: complete\ncommits: {run_id}-commit\n"
        )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": "complete" if terminal else "working",
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "process_alive": None,
        },
    )


def _finish(home: Path, run_id: str) -> None:
    pointer = crew.read_pointer(run_id)
    manifest = Path(pointer["manifest_path"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {run_id}\nstatus: complete\ncommits: {run_id}-commit\n")
    pointer["phase"] = "complete"
    crew._write_json(crew.pointer_path(run_id), pointer)


def _terminal_pointer(home: Path, tmp_path: Path, run_id: str) -> dict:
    worktree = tmp_path / run_id
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    (worktree / "result.txt").write_text("base\n")
    subprocess.run(["git", "add", "result.txt"], cwd=worktree, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test: establish fixture",
        ],
        cwd=worktree,
        check=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": "complete",
        "created_at": crew._utc_now(),
        "manifest_path": str(home / "runs" / run_id / "manifest.md"),
        "log_path": str(home / "runs" / run_id / "stream.jsonl"),
        "stderr_path": str(home / "runs" / run_id / "stderr.log"),
        "worktree": str(worktree),
        "base_sha": base,
        "final_message": None,
        "process_alive": False,
    }
    crew._write_json(crew.pointer_path(run_id), pointer)
    return pointer


def test_recover_derives_repair_evidence_without_promotion_advice(
    home, tmp_path, monkeypatch
) -> None:
    pointer = _terminal_pointer(home, tmp_path, "r-repairable")
    (Path(pointer["worktree"]) / "result.txt").write_text("correct work\n")
    pointer["final_message"] = "Implemented the requested behavior and tests passed."
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )

    result = recovery.recover(
        project="proj", config={"fences": {"manifest_required": True}}
    )

    row = result["runs"][0]
    manifest = Path(pointer["manifest_path"])
    text = manifest.read_text()
    assert "derived: true" in text
    assert "changed_paths: result.txt" in text
    assert "Implemented the requested behavior" in text
    assert row["classification"] == "abandoned"
    assert row["manifest_derived"] is True
    assert row["manifest_present"] is False
    assert "reckon crew complete" not in row["next_action"]
    assert row["next_action"].startswith("reckon crew resume --run r-repairable")
    gap = crew.read_pointer(pointer["run_id"])["delivery_gap"]
    assert gap["kind"] == "missing-worker-manifest"
    assert gap["derived_manifest_path"] == str(manifest)
    assert gap["final_message_present"] is True
    assert gap["changed_paths"] == ["result.txt"]


def test_recover_leaves_an_empty_terminal_run_abandoned(
    home, tmp_path, monkeypatch
) -> None:
    pointer = _terminal_pointer(home, tmp_path, "r-empty")
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )

    result = recovery.recover(project="proj")

    row = result["runs"][0]
    assert row["classification"] == "abandoned"
    assert row["manifest_derived"] is False
    assert not Path(pointer["manifest_path"]).exists()
    assert "delivery_gap" not in crew.read_pointer(pointer["run_id"])


def test_recover_never_overwrites_a_worker_manifest(
    home, tmp_path, monkeypatch
) -> None:
    pointer = _terminal_pointer(home, tmp_path, "r-delivered")
    manifest = Path(pointer["manifest_path"])
    manifest.parent.mkdir(parents=True)
    delivered = "node: r-delivered\nstatus: complete\ncommits: abc123\n"
    manifest.write_text(delivered)
    pointer["manifest_baseline_mtime_ns"] = 0
    pointer["final_message"] = "A final message that must not replace delivery."
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )

    result = recovery.recover(project="proj")

    assert manifest.read_text() == delivered
    assert result["runs"][0]["classification"] == "completed_unpromoted"
    assert result["runs"][0]["manifest_derived"] is False
    assert "delivery_gap" not in crew.read_pointer(pointer["run_id"])


def test_follow_watch_emits_three_terminal_runs_once_then_ends(home) -> None:
    _write_pointer(home, "r-first", terminal=True)
    _write_pointer(home, "r-second", terminal=False)
    _write_pointer(home, "r-third", terminal=False)
    sleeps = 0

    def advance_fleet(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            _finish(home, "r-second")
        elif sleeps == 2:
            _finish(home, "r-third")
        elif sleeps == 3:
            for run_id in ("r-first", "r-second", "r-third"):
                crew.pointer_path(run_id).unlink()
        else:
            pytest.fail("follow watch did not end when the fleet emptied")

    events = list(
        recovery.watch_follow(
            "proj", stall_window="1h", poll_interval=0, sleeper=advance_fleet
        )
    )

    assert [event["run_id"] for event in events] == [
        "r-first",
        "r-second",
        "r-third",
    ]
    assert [event["event"] for event in events] == ["terminal"] * 3
    assert sleeps == 3


def test_unpromoted_run_does_not_repeat_or_mask_a_later_terminal_run(home) -> None:
    _write_pointer(home, "r-unpromoted", terminal=True)
    _write_pointer(home, "r-later", terminal=False)
    sleeps = 0

    def finish_later(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            _finish(home, "r-later")
        elif sleeps == 2:
            assert crew.pointer_path("r-unpromoted").is_file()
            crew.pointer_path("r-unpromoted").unlink()
            crew.pointer_path("r-later").unlink()
        else:
            pytest.fail("an already reported pointer woke the watcher again")

    events = list(
        recovery.watch_follow(
            "proj", stall_window="1h", poll_interval=0, sleeper=finish_later
        )
    )

    assert [event["run_id"] for event in events] == ["r-unpromoted", "r-later"]
    assert sleeps == 2


def test_follow_watch_arms_on_empty_before_the_first_pointer(home) -> None:
    sleeps = 0

    def deliver_then_reconcile(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            _write_pointer(home, "r-first", terminal=True)
        elif sleeps == 2:
            crew.pointer_path("r-first").unlink()
        else:
            pytest.fail("watch did not stop after reconciling the first fleet")

    events = list(
        recovery.watch_follow(
            "proj", stall_window="1h", poll_interval=0, sleeper=deliver_then_reconcile
        )
    )

    assert [event["run_id"] for event in events] == ["r-first"]
    assert sleeps == 2


def test_cli_follow_streams_each_event_as_one_json_document(home, monkeypatch) -> None:
    events = (
        {"project": "proj", "event": "terminal", "run_id": run_id}
        for run_id in ("r-one", "r-two")
    )
    monkeypatch.setattr(recovery, "watch_follow", lambda *_args, **_kwargs: events)

    result = CliRunner().invoke(
        cli_module.main,
        ["crew", "watch", "--project", "proj", "--follow"],
    )

    payloads = [json.loads(line) for line in result.output.splitlines()]
    assert result.exit_code == 0
    assert [payload["run_id"] for payload in payloads] == ["r-one", "r-two"]
    assert all(payload["ok"] is True for payload in payloads)


def _snapshot_pointer(
    home: Path,
    run_id: str,
    *,
    phase: str,
    alive: bool | None,
    manifest_status: str | None = None,
) -> dict:
    manifest = home / "manifests" / f"{run_id}.md"
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "streams" / f"{run_id}.jsonl"),
        "process_alive": alive,
    }
    if manifest_status:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(f"node: {run_id}\nstatus: {manifest_status}\n")
    return recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)


def test_dead_process_never_counts_as_working_at_any_phase(home) -> None:
    # A run whose process is gone must leave the working bucket no matter which
    # phase its pointer last held, the starting phase the measured incident
    # never advanced past included. Counting it as working is the lie this
    # section exists to retire: five runs died at starting and read as working
    # for ten minutes.
    phases = (
        "",
        "starting",
        "working",
        "running",
        "complete",
        "failed",
        "stopped",
        "blocked",
        "orphaned",
    )
    for phase in phases:
        snapshot = _snapshot_pointer(
            home, f"r-dead-{phase or 'empty'}", phase=phase, alive=False
        )
        assert snapshot["state"] not in recovery.FLEET_WORKING_STATES, (
            f"a dead process at phase {phase!r} read as {snapshot['state']!r}, "
            "which counts as working"
        )


def test_blocked_manifest_never_counts_as_working(home) -> None:
    # A block asks the coordinator for action even if its process has not exited.
    snapshot = _snapshot_pointer(
        home,
        "r-term-blocked",
        phase="working",
        alive=True,
        manifest_status="blocked",
    )
    assert snapshot["state"] not in recovery.FLEET_WORKING_STATES


@pytest.mark.parametrize(
    ("status", "stopped_classification", "stopped_state"),
    [
        ("complete", "completed_unpromoted", "complete"),
        ("failed", "failed", "failed"),
    ],
)
def test_manifest_outcome_waits_until_the_pointer_says_the_process_stopped(
    home, status, stopped_classification, stopped_state
) -> None:
    # A manifest report is provisional while its writer remains alive, and
    # becomes an outcome only once the pointer says the process stopped. The
    # failure arm is also the negative guard: a genuine stopped failure retains
    # its existing classification and reason.
    manifest = home / "manifests" / f"r-live-{status}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: r-live-{status}\nstatus: {status}\nblockers: implementation failed\n"
    )
    pointer = {
        "run_id": f"r-live-{status}",
        "project": "proj",
        "node": {"id": f"r-live-{status}", "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "manifest_path": str(manifest),
        "log_path": str(home / "streams" / f"r-live-{status}.jsonl"),
        "process_alive": True,
    }

    live_row = recovery.classify_pointer(pointer, now_seconds=time.time())
    live_snapshot = recovery._watch_snapshot(
        pointer, moment=time.time(), stall_seconds=3600
    )
    assert live_row["classification"] == "running"
    assert live_row["process_alive"] is True
    assert live_row["manifest_status"] is None
    assert live_snapshot["state"] == "working"

    pointer["process_alive"] = False
    stopped_row = recovery.classify_pointer(pointer, now_seconds=time.time())
    stopped_snapshot = recovery._watch_snapshot(
        pointer, moment=time.time(), stall_seconds=3600
    )
    assert stopped_row["classification"] == stopped_classification
    assert stopped_row["manifest_status"] == status
    assert stopped_snapshot["state"] == stopped_state
    if status == "failed":
        assert "implementation failed" in stopped_row["detail"]


def test_live_process_outgrows_a_blocked_placeholder(home) -> None:
    manifest = home / "manifests" / "r-live-blocked.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: r-live-blocked\n"
        "status: blocked\n"
        "blockers: implementation and required evidence are not yet complete\n"
    )
    stream = home / "streams" / "r-live-blocked.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest_mtime = manifest.stat().st_mtime_ns
    os.utime(stream, ns=(manifest_mtime + 1, manifest_mtime + 1))
    assert stream.stat().st_mtime_ns > manifest.stat().st_mtime_ns
    pointer = {
        "run_id": "r-live-blocked",
        "project": "proj",
        "node": {"id": "r-live-blocked", "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "process_alive": True,
    }

    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)

    assert row["classification"] == "running"
    assert row["manifest_status"] is None
    assert snapshot["state"] == "working"


def test_fleet_counts_still_partition_the_runs_in_flight(home) -> None:
    # A live worker, a run that died at starting, and a delivered-but-unpromoted
    # run each land in exactly one bucket, and the three buckets still add back
    # to the number of runs in flight — a figure a reader adds up must add up.
    snapshots = {
        "r-live": _snapshot_pointer(home, "r-live", phase="working", alive=True),
        "r-dead-starting": _snapshot_pointer(
            home, "r-dead-starting", phase="starting", alive=False
        ),
        "r-done": _snapshot_pointer(
            home, "r-done", phase="working", alive=False, manifest_status="complete"
        ),
    }
    counts = recovery._fleet_counts(snapshots)
    assert counts == {"working": 1, "blocked": 1, "unpromoted": 1}
    assert sum(counts.values()) == len(snapshots)


def test_observe_and_watch_render_failure_only_after_the_process_stops(
    tmp_path, monkeypatch
) -> None:
    # Exercise the two production boundaries together. Observe persists the
    # process fact onto the live pointer; the producer reduces that pointer and
    # the follower renders its stored transition. No one re-probes liveness in
    # between those boundaries.
    fallback_home = tmp_path / "fallback-home"
    monkeypatch.setenv("HOME", str(fallback_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("RECKON_HOME", raising=False)
    untouched_home = fallback_home / ".config" / "reckon"

    config_home = tmp_path / "isolated-config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    run_id = "r-live-failure"
    stream = config_home / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = config_home / "runs" / run_id / "manifest.md"
    manifest.write_text(
        "node: r-live-failure\n"
        "status: failed\n"
        "blockers: implementation and required evidence are not yet complete\n"
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": datetime.now(tz=UTC).isoformat(),
            "pid": 4242,
            "process_alive": False,
            "manifest_path": str(manifest),
            "log_path": str(stream),
            "stderr_path": str(config_home / "runs" / run_id / "stderr.log"),
        },
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    liveness = iter((True, False))
    monkeypatch.setattr(dispatch_module, "process_alive", lambda _pid: next(liveness))

    live_pointer = crew.observe(run_id)
    assert live_pointer["process_alive"] is True
    assert recovery.classify_pointer(live_pointer)["classification"] == "running"
    assert crew._watch_event("proj", stall_seconds=3600) is None

    producer = runs._WatchStreamProducer(
        path=runs.watch_stream_path("proj"), known={}, stall_window="1h"
    )
    runs._WATCH_STREAM_PRODUCERS["proj"] = producer
    try:
        crew.list_live(project="proj")
        stopped_pointer = crew.observe(run_id)
        crew.list_live(project="proj")
    finally:
        runs._WATCH_STREAM_PRODUCERS.pop("proj", None)

    events = list(runs.read_stream_events(producer.path))
    assert [(event["event"], event["to_state"]) for event in events] == [
        ("baseline", "working"),
        ("transition", "failed"),
    ]
    assert events[1]["from_state"] == "working"
    assert stopped_pointer["process_alive"] is False
    assert recovery.classify_pointer(stopped_pointer)["classification"] == "failed"
    terminal_event = crew._watch_event("proj", stall_seconds=3600)
    assert terminal_event is not None
    assert terminal_event["event"] == "terminal"
    assert terminal_event["classification"] == "failed"

    rendered = [recovery.format_watch_transition(event) for event in events]
    assert "working" in rendered[0]
    assert "failed" in rendered[1]
    assert "implementation and required evidence" in rendered[1]
    assert not untouched_home.exists()


def _cli_pointer(
    home: Path, run_id: str, stream: str, *, manifest: str | None = None, **kw
) -> dict:
    """A live cli pointer whose stream is one of the recorded backend fixtures.

    No budget is folded in by default, so the refusal gate has to read the
    stream itself — the raw path the ticker takes — and the crash fixtures
    exercise the decline half of the gate.
    """
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "backend": "codex",
        "launch": "cli",
        "argv": ["codex"],
        "log_path": str(BACKEND_FIXTURES / stream),
        "stderr_path": str(home / "stderr.log"),
        "phase": "starting",
        "process_alive": False,
        "session": "s",
        "manifest_path": str(home / "manifests" / f"{run_id}.md"),
    }
    if manifest is not None:
        manifest_path = home / "manifests" / f"{run_id}.md"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest)
    record.update(kw)
    return record


def test_refusal_stream_classifies_blocked_not_abandoned(home) -> None:
    # A pointer whose stream records a provider refusal must block, never read
    # as abandoned (a state implying nothing can be done) and never complete.
    pointer = _cli_pointer(home, "r-refused", "codex-usage-limit.jsonl")
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "blocked"
    assert row["classification"] not in {"completed_unpromoted", "abandoned"}
    assert "codex" in row["detail"]
    assert "refused the turn on a usage-limit" in row["detail"]
    assert "resume" in row["next_action"]


def test_refusal_block_matches_the_phase_observe_reports(home) -> None:
    # The two surfaces that can disagree answer from the same stream translation.
    pointer = _cli_pointer(home, "r-refused", "codex-usage-limit.jsonl")
    raw_row = recovery.classify_pointer(pointer, now_seconds=time.time())
    observation = _backends.observe_log(
        backend_name="codex",
        backend={"command": "codex"},
        log_path=str(BACKEND_FIXTURES / "codex-usage-limit.jsonl"),
    )
    assert observation.phase == "blocked"
    folded = _cli_pointer(
        home,
        "r-refused-fold",
        "codex-usage-limit.jsonl",
        budget=observation.as_dict()["budget"],
    )
    folded_row = recovery.classify_pointer(folded, now_seconds=time.time())
    assert raw_row["classification"] == "blocked"
    assert folded_row["classification"] == "blocked"
    assert raw_row["classification"] == folded_row["classification"] == "blocked"
    assert observation.budget["rate_limit_type"] == "usage-limit"
    assert "usage-limit" in raw_row["detail"]


def test_refusal_block_detail_states_nothing_delivered_without_a_manifest(home) -> None:
    pointer = _cli_pointer(home, "r-nomanifest", "codex-usage-limit.jsonl")
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "blocked"
    assert "no manifest was delivered" in row["detail"]
    assert "records what was already delivered" not in row["detail"]


def test_refusal_block_names_delivery_from_an_unverdict_manifest(home) -> None:
    # A manifest that never reached a verdict still records what landed, so the
    # block must name that delivery rather than imply nothing was produced.
    pointer = _cli_pointer(
        home,
        "r-inprogress",
        "codex-usage-limit.jsonl",
        manifest="node: r-inprogress\nstatus: in-progress\n"
        "artifacts: two suite logs, per-test ledgers\n",
    )
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "blocked"
    assert "records what was already delivered" in row["detail"]
    assert "no manifest was delivered" not in row["detail"]


def test_a_crash_without_a_refusal_still_abandons(home) -> None:
    # The negative: a genuine crash carries no recognised limit phrase, so it
    # must stay abandoned and never be promoted to a block by a terminal stream.
    pointer = _cli_pointer(home, "r-crash", "codex-failed-turn.jsonl")
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "abandoned"
    observation = _backends.observe_log(
        backend_name="codex",
        backend={"command": "codex"},
        log_path=str(BACKEND_FIXTURES / "codex-failed-turn.jsonl"),
    )
    assert observation.budget.get("refusal") is not True


# ── A background wait is not a vanished process ────────────────────────────
#
# Both fixtures below are transcribed verbatim from run directories a live
# fleet actually wrote (a print-mode Claude Code CLI worker whose background
# task outlived its own turn), not from a paraphrase of what such a run might
# write. The match this classifier makes is on what the harness writes.

# The literal stderr line, byte for byte, from a run that hit the ceiling.
_BACKGROUND_WAIT_CEILING_STDERR = (
    "Background tasks still running after 600s; terminating. Set "
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely."
)

# The literal `result` field, byte for byte, from a run whose last turn ended
# normally (is_error false, subtype success) while a background suite was
# still running — the print-mode invocation then had no next turn to write.
_BACKGROUND_WAIT_RESULT_TEXT = (
    "Waiting for the background `tests/standard_names` suite run to "
    "complete before finalizing the manifest."
)


def _claude_stream_pointer(
    home: Path,
    run_id: str,
    events: list[dict],
    *,
    manifest: str | None = None,
    **kw,
) -> dict:
    """A live cli pointer over a Claude Code print-mode stream built in place.

    The refusal fixtures above read a recorded backend log from disk; this
    node's write scope holds no fixtures directory, so the stream this
    reproduces is written inline from the exact quoted production text
    instead, into the same temporary home every other pointer in this file
    uses.
    """
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("".join(json.dumps(event) + "\n" for event in events))
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "backend": "claude",
        "launch": "cli",
        "argv": ["claude"],
        "log_path": str(stream),
        "stderr_path": str(home / f"{run_id}-stderr.log"),
        "phase": "working",
        "process_alive": False,
        "session": "s",
        "manifest_path": str(home / "manifests" / f"{run_id}.md"),
    }
    if manifest is not None:
        manifest_path = home / "manifests" / f"{run_id}.md"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest)
    record.update(kw)
    return record


def test_stderr_wait_ceiling_classifies_blocked_not_abandoned(home) -> None:
    # First measured signal: the process is gone, no manifest was ever
    # written, and the only trace is the harness's own ceiling line on
    # stderr. The intact session makes the stop triageable: blocking and naming
    # the wait directs a resume that can collect the manifest.
    pointer = _claude_stream_pointer(home, "r-ceiling", [])
    (Path(pointer["stderr_path"])).write_text(_BACKGROUND_WAIT_CEILING_STDERR)
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "blocked"
    assert row["classification"] not in {"abandoned", "completed_unpromoted"}
    assert "background-wait ceiling" in row["detail"]
    assert "no manifest was delivered" in row["detail"]
    assert row["next_action"] == f"reckon crew resume --run {pointer['run_id']}"
    assert "redispatch" not in row["next_action"]


def test_final_message_background_wait_classifies_blocked_not_abandoned(
    home,
) -> None:
    # Second measured signal: the worker's own last turn ended normally
    # (is_error false, subtype success) while stating it was waiting on
    # background work, and a print-mode invocation has no turn after that one
    # to write the manifest it was about to write. Nothing terminated it.
    pointer = _claude_stream_pointer(
        home,
        "r-waiting",
        [
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "result": _BACKGROUND_WAIT_RESULT_TEXT,
            }
        ],
    )
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "blocked"
    assert row["classification"] not in {"abandoned", "completed_unpromoted"}
    assert "waiting on background work" in row["detail"]
    assert "standard_names" in row["detail"]
    assert row["next_action"] == f"reckon crew resume --run {pointer['run_id']}"


def test_background_wait_names_delivery_from_an_unverdict_manifest(home) -> None:
    # Delivery reporting matches the refusal block's: a manifest that never
    # reached a verdict still names what it holds rather than implying
    # nothing landed.
    pointer = _claude_stream_pointer(
        home,
        "r-waiting-partial",
        [
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "result": _BACKGROUND_WAIT_RESULT_TEXT,
            }
        ],
        manifest="node: r-waiting-partial\nstatus: in-progress\n"
        "artifacts: two suite logs\n",
    )
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "blocked"
    assert "records what was already delivered" in row["detail"]
    assert "no manifest was delivered" not in row["detail"]


def test_background_wait_survives_the_observe_fold(home, monkeypatch) -> None:
    # observe() would fold this stream to phase "complete" (a terminal result
    # with is_error false), which the terminal branch would otherwise read as
    # abandoned for want of a manifest. The background-wait check must win
    # ahead of that branch, exercised here through recover() the same way
    # observe()'s caller would reach classify_pointer.
    pointer = _claude_stream_pointer(
        home,
        "r-waiting-folded",
        [
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "result": _BACKGROUND_WAIT_RESULT_TEXT,
            }
        ],
        phase="complete",
        final_message=_BACKGROUND_WAIT_RESULT_TEXT,
    )
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )

    result = recovery.recover(project="proj", config={})

    row = result["runs"][0]
    assert row["classification"] == "blocked"
    assert row["classification"] not in {"abandoned", "completed_unpromoted"}
    assert "waiting on background work" in row["detail"]


def test_background_wait_snapshots_as_blocked_in_the_watch_producer_path(
    home,
) -> None:
    # The watch producer never calls observe(); it reduces the raw pointer
    # straight through classify_pointer, which is the path the two measured
    # runs actually hit. Both signals must reach the same state there too.
    ceiling_pointer = _claude_stream_pointer(home, "r-ceiling-watch", [])
    Path(ceiling_pointer["stderr_path"]).write_text(_BACKGROUND_WAIT_CEILING_STDERR)
    ceiling_snapshot = recovery._watch_snapshot(
        ceiling_pointer, moment=time.time(), stall_seconds=3600
    )
    assert ceiling_snapshot["state"] == "blocked"
    assert ceiling_snapshot["state"] in recovery.FLEET_BLOCKED_STATES
    assert "background-wait ceiling" in ceiling_snapshot["detail"]

    waiting_pointer = _claude_stream_pointer(
        home,
        "r-waiting-watch",
        [
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "result": _BACKGROUND_WAIT_RESULT_TEXT,
            }
        ],
    )
    waiting_snapshot = recovery._watch_snapshot(
        waiting_pointer, moment=time.time(), stall_seconds=3600
    )
    assert waiting_snapshot["state"] == "blocked"
    assert waiting_snapshot["state"] in recovery.FLEET_BLOCKED_STATES
    assert "waiting on background work" in waiting_snapshot["detail"]


def test_a_vanished_process_with_neither_wait_signal_stays_abandoned(home) -> None:
    # The negative the done-when requires: a run that is gone, carrying
    # neither the stderr ceiling nor a background-wait final message, keeps
    # its present classification. Otherwise the new arm becomes the
    # catch-all "unreadable" was written to guard against.
    pointer = _claude_stream_pointer(home, "r-plain-crash", [])
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "abandoned"
    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] == "abandoned"


# ── An unreadable manifest is its own outcome ──────────────────────────────


def test_a_truncated_json_manifest_classifies_unreadable_not_abandoned(home) -> None:
    # A worker dying mid-write leaves a truncated manifest that declares JSON
    # and is not a readable object. The classifier must degrade to a named
    # refusal rather than raising — an escaped exception would fail every
    # ticker refresh for every session — and must not fold into abandoned,
    # which implies nothing can be done about a file that needs repair.
    pointer = _cli_pointer(
        home,
        "r-trunc",
        "codex-failed-turn.jsonl",
        manifest='{"node": "x", "status": "complete", ',
        process_alive=False,
    )
    row = recovery.classify_pointer(pointer, now_seconds=time.time())
    assert row["classification"] == "unreadable"
    assert row["classification"] not in {"abandoned", "completed_unpromoted"}
    assert row["manifest_present"] is True
    assert row["manifest_error"]
    assert "could not be read" in row["detail"]
    assert "JSON" in row["manifest_error"]


def test_the_three_manifest_outcomes_stay_distinct(home) -> None:
    # Absent, readable-and-terminal and present-but-unreadable each reach the
    # classifier as their own label, so a future collapse into one of the
    # other two fails this assertion instead of the display.
    absent = _cli_pointer(home, "r-absent", "codex-failed-turn.jsonl")
    truncated = _cli_pointer(
        home,
        "r-truncated",
        "codex-failed-turn.jsonl",
        manifest='{"node": "x", "status": "complete", ',
    )
    readable = _cli_pointer(
        home,
        "r-readable",
        "codex-failed-turn.jsonl",
        manifest="node: r-readable\nstatus: complete\ncommits: abc\n",
        phase="complete",
    )
    labels = {
        recovery.classify_pointer(p, now_seconds=time.time())["classification"]
        for p in (absent, truncated, readable)
    }
    assert labels == {"abandoned", "unreadable", "completed_unpromoted"}


def test_unreadable_pointer_snapshots_unreadable_in_the_ticker_path(home) -> None:
    # The ticker's state reduction must keep the third outcome distinct too:
    # a dead process with an unreadable manifest reads as unreadable, never as
    # abandoned (the bucket the liveness checks would otherwise assign it).
    truncated = _cli_pointer(
        home,
        "r-trunc",
        "codex-failed-turn.jsonl",
        manifest='{"node": "x", "status": "complete", ',
        process_alive=False,
    )
    snapshot = recovery._watch_snapshot(
        truncated, moment=time.time(), stall_seconds=3600
    )
    assert snapshot["state"] == "unreadable"
    assert snapshot["state"] not in recovery.FLEET_WORKING_STATES
    assert snapshot["state"] not in recovery.FLEET_UNPROMOTED_STATES
    # The refusal survives into the snapshot's detail rather than being cleared
    # with the routine-progress states. The clause is elided to the ticker
    # width, so assert it names the file rather than a substring that a long
    # path may push past the elision boundary.
    assert snapshot["detail"]
    assert "manifest" in snapshot["detail"]
    # The negative for the new arm: a dead process that delivered no manifest
    # at all still abandons, so unreadable cannot become the catch-all that
    # abandoned would be folded into.
    absent = _cli_pointer(
        home, "r-nothing", "codex-failed-turn.jsonl", process_alive=False
    )
    absent_snapshot = recovery._watch_snapshot(
        absent, moment=time.time(), stall_seconds=3600
    )
    assert absent_snapshot["state"] == "abandoned"


def test_refusal_blocked_pointer_snapshots_as_blocked_in_the_ticker_path(home) -> None:
    pointer = _cli_pointer(home, "r-refused", "codex-usage-limit.jsonl")
    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] == "blocked"
    assert snapshot["state"] in recovery.FLEET_BLOCKED_STATES
    assert "usage-limit" in snapshot["detail"]
    crash = _cli_pointer(home, "r-crash", "codex-failed-turn.jsonl")
    crash_snapshot = recovery._watch_snapshot(
        crash, moment=time.time(), stall_seconds=3600
    )
    assert crash_snapshot["state"] == "abandoned"


def test_single_event_watch_still_returns_the_first_terminal_run(home) -> None:
    _write_pointer(home, "r-first", terminal=True)
    _write_pointer(home, "r-second", terminal=True)

    event = crew.watch("proj", stall_window="1h")

    assert event["event"] == "terminal"
    assert event["run_id"] == "r-first"
    assert crew.pointer_path("r-first").is_file()
    assert crew.pointer_path("r-second").is_file()


# ── The reason says what the worker asked ───────────────────────────────────


def _blocked_record(home: Path, run_id: str, *, manifest_text: str) -> dict:
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(manifest_text)
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "streams" / f"{run_id}.jsonl"),
        "process_alive": True,
    }


_NEEDS_HELP_BLOCK = """\
node: r-node
status: blocked
blockers: |
  NEEDS-HELP: the schema rejects an enum value the config file needs
  tried: set gates.enforce to off; validation rejected the boolean
  options: spell the value disabled; or coerce the boolean
  leaning: spell it disabled, because the coercion would be invisible
  cost-if-wrong: the generated schema and its committed JSON regenerate
"""

_BLOCKER_ONLY_BLOCK = """\
node: r-node
status: blocked
blockers: |
  the credential file is missing on this host and dispatch cannot proceed
"""


def test_a_complete_needs_help_report_becomes_the_reason_with_a_question_marker(
    home,
) -> None:
    record = _blocked_record(home, "r-asked", manifest_text=_NEEDS_HELP_BLOCK)

    row = recovery.classify_pointer(record, now_seconds=time.time())

    assert row["classification"] == "blocked"
    assert row["marker"] == "?"
    assert row["needs_help_complete"] is True
    assert "the schema rejects an enum value" in row["detail"]
    assert row["detail"].strip() != "|"

    snapshot = recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] == "blocked"
    assert snapshot["needs_help_complete"] is True
    assert (
        snapshot["detail"] == "the schema rejects an enum value the config file needs"
    )

    transition = recovery._watch_transition(
        "proj",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({"r-asked": snapshot}),
    )
    assert transition["needs_help_complete"] is True
    assert (
        transition["detail"] == "the schema rejects an enum value the config file needs"
    )

    # The glyph is derived at render time from the persisted fact.
    line = recovery.format_watch_transition(transition)
    assert "?" in line
    assert "the schema rejects an enum value" in line


def test_blockers_without_a_needs_help_report_get_the_blocker_text_and_a_bang_marker(
    home,
) -> None:
    record = _blocked_record(home, "r-blocked", manifest_text=_BLOCKER_ONLY_BLOCK)

    row = recovery.classify_pointer(record, now_seconds=time.time())

    assert row["classification"] == "blocked"
    assert row["marker"] == "!"
    assert row["needs_help_complete"] is False
    assert "the credential file is missing" in row["detail"]

    snapshot = recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] == "blocked"
    assert snapshot["needs_help_complete"] is False
    assert snapshot["detail"] == (
        "the credential file is missing on this host and dispatch cannot proceed"
    )


def test_a_block_scalar_indicator_is_never_read_as_the_value(home) -> None:
    # The measured incident this section retires: a manifest's `blockers:`
    # value was the literal block-scalar indicator, and that single
    # punctuation character rode all the way into a stored transition's
    # reason.
    manifest_text = "node: r-empty-block\nstatus: blocked\nblockers: |\n"
    record = _blocked_record(home, "r-empty-block", manifest_text=manifest_text)

    row = recovery.classify_pointer(record, now_seconds=time.time())

    assert row["marker"] == "!"
    assert row["detail"].strip() != "|"
    assert "|" not in row["detail"]

    snapshot = recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)
    assert snapshot["detail"] != "|"


@pytest.mark.parametrize("bare", ["|", ">", '"', "'", "|-", ">+"])
def test_single_clause_refuses_a_bare_punctuation_reason(bare) -> None:
    # The refusal moved to the derive site (the renderer), where the clause is
    # actually produced, so it lives beside the single_clause it guards.
    assert ticker_module.single_clause(bare) == ""


def test_a_block_scalar_blockers_value_is_parsed_from_its_indented_body() -> None:
    fields = reports.parse_manifest(_NEEDS_HELP_BLOCK)
    assert fields["blockers"] != ["|"]
    assert any("credential" not in item for item in fields["blockers"])
    assert fields["needs_help"]["complete"] is True
    assert fields["needs_help"]["headline"] == (
        "the schema rejects an enum value the config file needs"
    )


@pytest.mark.parametrize("indicator", ["|", "|-", "|+", ">", ">-", ">+", '"', "'"])
def test_parse_manifest_reads_the_indented_body_not_the_indicator(indicator) -> None:
    text = (
        f"node: r-node\nstatus: blocked\nblockers: {indicator}\n"
        "  the actual blocker text lives here\n"
    )

    fields = reports.parse_manifest(text)

    assert fields["blockers"] == ["the actual blocker text lives here"]


def test_parse_manifest_stops_the_block_at_the_next_top_level_key() -> None:
    text = (
        "node: r-node\nstatus: blocked\nblockers: |\n"
        "  the first line of the block\n"
        "  the second line of the block\n"
        "commits: abc123\n"
    )

    fields = reports.parse_manifest(text)

    assert fields["blockers"] == [
        "the first line of the block",
        "the second line of the block",
    ]
    assert fields["commits"] == ["abc123"]


# ── The snapshot threads the role it renders ────────────────────────────────


def _role_snapshot(
    home: Path,
    run_id: str,
    *,
    role: str | None = None,
    node_role: str | None = None,
    manifest_status: str | None = None,
    agent: Mapping[str, Any] | None = None,
) -> dict:
    """Reduce a pointer built from scratch, so the role field is the only
    difference between tests and cannot be smuggled in by a shared fixture."""
    manifest = home / "manifests" / f"{run_id}.md"
    node = {"id": run_id, "plan": "plan-a", "time_budget": "20m"}
    if node_role:
        node["role"] = node_role
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": node,
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "streams" / f"{run_id}.jsonl"),
        "process_alive": True,
    }
    if role:
        record["role"] = role
    if agent is not None:
        record["agent"] = agent
    if manifest_status:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(f"node: {run_id}\nstatus: {manifest_status}\n")
    return recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)


def _role_transition(home: Path, run_id: str, **role_kwargs) -> dict:
    snapshot = _role_snapshot(home, run_id, **role_kwargs)
    return recovery._watch_transition(
        "proj",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({run_id: snapshot}),
    )


def test_transition_carries_the_role_its_pointer_carried(home) -> None:
    # Built from a pointer rather than a synthetic event, so this cannot pass
    # while the role field is simply absent from the snapshot.
    transition = _role_transition(home, "r-role", role="implement")

    assert transition["role"] == "implement"
    line = recovery.format_watch_transition(transition)
    assert "impl" in line


def test_the_role_stamped_on_the_node_reaches_the_transition_tool(home) -> None:
    # Dispatch writes the role both on the record root and on the node; both
    # spellings are the same fact, so both must thread.
    transition = _role_transition(home, "r-node-role", node_role="test")

    assert transition["role"] == "test"


def test_documentation_is_narrowed_to_docs_by_the_renderer(home) -> None:
    transition = _role_transition(home, "r-doc", role="documentation")

    assert transition["role"] == "documentation"
    line = recovery.format_watch_transition(transition)
    assert "docu" in line


def test_a_pointer_without_a_role_renders_the_marker_without_raising(home) -> None:
    transition = _role_transition(home, "r-norole")

    assert transition["role"] == ""
    line = recovery.format_watch_transition(transition)
    # An unknown role is marked, not truncated, so a reader is never invited to
    # guess the rest of a word that says what kind of work this is.
    assert "?" in line


# The fixed-grid ticker reads one display key per laid-out column, plus the
# reason clause and the three fleet counters, all off the transition event. A
# column added later that reads a key the snapshot never threads would render
# its marker forever while the suite stayed green — the treadmill this section
# exists to close — so the contract is that every key the renderer consumes is
# carried on every transition it is given. That read surface is not only the
# laid-out columns: the row's kind routes it to the baseline form and the block
# glyph is derived from a persisted fact, and lineage drives whole-row dimming
# rather than occupying any column, so a presence check scoped to the column set
# would pass while the dimming fact never reached the event. Every read-by-any-
# means field belongs in the carried set. The facts the display is derived from
# travel under their own names, and the display-shaped fields themselves must
# never reappear on the record.
_TICKER_READ_FIELDS = (
    "observed_at",
    "event",
    "role",
    "node",
    "run_id",
    "session",
    "to_state",
    "from_state",
    "backend",
    "model",
    "effort",
    "alias",
    "detail",
    "needs_help_complete",
    "lineage",
    "working",
    "blocked",
    "unpromoted",
)

# A display-shaped field is exactly what the log must not persist: the composed
# agent label, a pre-claused reason, the marker glyph, and the flattened shadow
# flag are all derived by the renderer from the facts above (lineage chief among
# them). A new-shape line carries the lineage it was shadowed under, never a
# boolean shorthand.
_DISPLAY_SHAPED_FIELDS = ("agent", "reason", "marker", "shadow")


def test_snapshot_carries_every_field_the_ticker_column_set_reads(home) -> None:
    # Constructed from a pointer whose manifest is blocked so the detail is
    # populated; a snapshot whose state supplied nothing to explain would not
    # exercise the reason slot the renderer reads. The aliased agent keeps the
    # presence check honest: a field that is present but reduces stale passes a
    # presence-only assertion, so the reduction must equal what the renderer
    # expects — the alias in its own column, not the model the record also
    # carries.
    snapshot = _role_snapshot(
        home,
        "r-contract",
        role="implement",
        manifest_status="blocked",
        agent={
            "backend": "claude",
            "launch": "cli",
            "model": "deepseek-v4-flash",
            "effort": "medium",
            "alias": "dsv4-flash",
            "effort_spelling": "me",
        },
    )
    transition = recovery._watch_transition(
        "proj",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({"r-contract": snapshot}),
    )

    for field in _TICKER_READ_FIELDS:
        assert field in transition, (
            f"the renderer reads {field!r} but the transition does not carry it"
        )
    for field in _DISPLAY_SHAPED_FIELDS:
        assert field not in transition, (
            f"the producer must not persist the display-shaped field {field!r}"
        )
    assert transition["model"] == "deepseek-v4-flash"
    assert transition["alias"] == "dsv4-flash"
    line = recovery.format_watch_transition(transition)
    assert "dsv4-flash" in line
    assert "deepseek-v4-flash" not in line
    assert "medium" in line
    assert "impl" in line


# ── The transition carries the shadow lineage it dims by ────────────────────


def _shadow_transition(home: Path, run_id: str, **lineage_overrides) -> dict:
    """Reduce a pointer carrying dispatch-written shadow lineage end to end.

    Dispatch decides shadowship at launch and writes the lineage — kind shadow
    plus the committed primary it derives from — onto the pointer. This builds
    that pointer by hand and threads it through the snapshot and the transition
    builders, so the emitted event is read rather than a synthetic one supplied
    to the renderer, which is how the dimming defect survived a green suite four
    times before.
    """
    lineage = {
        "kind": "shadow",
        "primary_run_id": f"{run_id}-primary",
        "configuration": {"substituted": [], "inherited": []},
    }
    lineage.update(lineage_overrides)
    manifest = home / "manifests" / f"{run_id}.md"
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "role": "implement",
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(home / "streams" / f"{run_id}.jsonl"),
        "process_alive": True,
        "lineage": lineage,
    }
    snapshot = recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)
    return recovery._watch_transition(
        "proj",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({run_id: snapshot}),
    )


def test_a_shadow_pointer_dims_the_row_it_emits(home) -> None:
    # The event built from a real shadow pointer carries the very lineage the
    # renderer reads, so a genuinely sourced shadow row dims end to end rather
    # than rendering like any other row.
    transition = _shadow_transition(home, "r-shadow")

    lineage = transition["lineage"]
    assert isinstance(lineage, Mapping)
    assert lineage.get("kind") == "shadow"
    assert lineage.get("primary_run_id") == "r-shadow-primary"
    assert ticker_module.is_shadow(transition) is True

    painter = ticker_module.Ticker(theme="light", color=True)
    shadow_line = painter.render(transition)
    # A shadow row reads dim end to end: every styled cell that would carry a
    # hue — the node and either side of the arrow among them — renders dim
    # instead, so the line carries no hue selector, while the identical row
    # with the lineage removed does.
    control = painter.render({**transition, "lineage": None})
    assert re.search(r"\x1b\[38;5;", control) is not None
    assert ticker_module._DIM in shadow_line
    assert re.search(r"\x1b\[38;5;", shadow_line) is None


def test_a_pointer_without_lineage_renders_undimmed_without_raising(home) -> None:
    # The negative: a run that was never shadowed carries no lineage, keeps
    # rendering normally, and neither the producer nor the renderer raises on
    # its absence.
    transition = _role_transition(home, "r-plain")

    assert transition.get("lineage") in (None, {})
    assert ticker_module.is_shadow(transition) is False
    painter = ticker_module.Ticker(theme="light", color=True)
    line = painter.render(transition)
    assert re.search(r"\x1b\[38;5;", line) is not None


def test_agent_label_returns_the_declared_alias_and_effort_spelling() -> None:
    # A run dispatched under an aliased backend carries alias and spelling beside
    # the model it shortens; both were decided at dispatch and must thread, not
    # be re-invented from the model and effort the pointing record still holds.
    pointer = {
        "agent": {
            "backend": "claude",
            "launch": "cli",
            "model": "deepseek-v4-flash",
            "effort": "medium",
            "alias": "dsv4-flash",
            "effort_spelling": "me",
        }
    }
    assert recovery.agent_label(pointer) == "dsv4-flash·me"


def test_a_pointer_without_an_alias_keeps_the_model_effort_form() -> None:
    # The backward-compatibility negative: a run recorded before aliases
    # existed (or a backend that never declared one) keeps the precomposed
    # model/effort string the pane rendered before, rather than a new form.
    pointer = {"agent": {"model": "deepseek-v4-flash", "effort": "medium"}}
    assert recovery.agent_label(pointer) == "deepseek-v4-flash/medium"


def test_an_aliased_pointer_renders_the_alias_not_the_model_id(home) -> None:
    # Screened through the snapshot and the renderer together, not the renderer
    # alone: the alias has to survive the pointer-to-snapshot reduction and then
    # the render, which is the path the measured bug dropped it on. The model
    # and effort travel as separate facts and both survive; the display shows
    # the alias and the full effort, never the model id underneath.
    snapshot = _role_snapshot(
        home,
        "r-alias",
        role="implement",
        manifest_status="blocked",
        agent={
            "backend": "claude",
            "launch": "cli",
            "model": "deepseek-v4-flash",
            "effort": "medium",
            "alias": "dsv4-flash",
            "effort_spelling": "me",
        },
    )
    transition = recovery._watch_transition(
        "proj",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({"r-alias": snapshot}),
    )
    assert transition["model"] == "deepseek-v4-flash"
    assert transition["alias"] == "dsv4-flash"
    assert transition["effort"] == "medium"
    assert "agent" not in transition
    line = recovery.format_watch_transition(transition)
    assert "dsv4-flash" in line
    assert "deepseek-v4-flash" not in line
    assert "medium" in line


# ── The log stores facts, the monitor derives the display ────────────────────


def _fact_snapshot(
    home: Path,
    run_id: str,
    *,
    manifest_status: str,
    agent: Mapping[str, Any] | None = None,
) -> dict:
    """A snapshot whose only moving parts are the facts the log must carry."""
    return _role_snapshot(
        home,
        run_id,
        role="implement",
        manifest_status=manifest_status,
        agent=agent
        or {
            "backend": "claude",
            "model": "deepseek-v4-flash",
            "effort": "medium",
            "alias": "dsv4-flash",
        },
    )


def _fact_transition(home: Path, run_id: str, **kw) -> dict:
    snapshot = _fact_snapshot(home, run_id, **kw)
    return recovery._watch_transition(
        "proj",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({run_id: snapshot}),
    )


def test_a_written_log_line_persists_facts_and_no_display_values(
    home, tmp_path
) -> None:
    # Read out of the events file, not off a function's return value: the log is
    # what a later reader gets, and the producer's returned object could carry a
    # field the writer drops or the reader loses.
    transition = _fact_transition(home, "r-facts", manifest_status="blocked")
    events = tmp_path / "proj.events"
    runs._append_watch_lines(events, [transition])

    written = list(runs.read_stream_events(events))
    assert len(written) == 1
    line = written[0]
    for key in ("backend", "model", "effort", "alias", "detail", "needs_help_complete"):
        assert key in line, f"the log line must carry the fact {key!r}"
    assert line["model"] == "deepseek-v4-flash"
    assert line["alias"] == "dsv4-flash"
    assert line["effort"] == "medium"
    assert line["detail"]  # the full untruncated detail is present
    for key in _DISPLAY_SHAPED_FIELDS:
        assert key not in line, (
            f"the log line must not persist the display-shaped field {key!r}"
        )


def test_legacy_log_line_renders_and_new_line_renders_one_fused_cell(home) -> None:
    # A real line taken from the project's events log, written before the facts
    # switch: it has only the composed agent, a pre-claused reason, and no raw
    # fields underneath, and must still render without raising.
    legacy = {
        "agent": "gpt-5.6-sol/medium",
        "blocked": 1,
        "event": "transition",
        "from_state": "dispatched",
        "node": "n-resolve-cross-module-refs",
        "observed_at": "2026-09-01T14:01:14Z",
        "project": "reckon",
        "reason": "the same focused pytest command failed twice after different fixes",
        "run_id": "r-1",
        "session": "d8-repair",
        "to_state": "blocked",
        "unpromoted": 0,
        "working": 0,
    }
    legacy_line = recovery.format_watch_transition(legacy)
    assert "gpt-5.6-sol/medium" in legacy_line
    assert "the same focused pytest command failed twice" in legacy_line

    # A line in the new shape renders the model and its effort as one routing
    # fact: a single cell, alias and effort word joined by exactly one
    # separator with no padding between them.
    transition = _fact_transition(
        home,
        "r-new",
        manifest_status="blocked",
        agent={
            "backend": "claude",
            "model": "deepseek-v4-flash",
            "effort": "high",
            "alias": "dsv4-flash",
        },
    )
    new_line = recovery.format_watch_transition(transition)
    assert "dsv4-flash·high" in new_line
    assert "dsv4-flash · high" not in new_line  # no padding around the separator


def test_one_stored_new_line_renders_differently_at_two_display_settings(home) -> None:
    # The re-renderable-history property: the same persisted facts redraw when
    # the display rule changes, without the producer running again. A narrower
    # grid gives the reason less room, so the stored line changes shape.
    transition = _fact_transition(home, "r-rerender", manifest_status="blocked")
    narrow = recovery.format_watch_transition(
        transition, ticker=ticker_module.Ticker(width=146, color=False)
    )
    wide = recovery.format_watch_transition(
        transition, ticker=ticker_module.Ticker(width=180, color=False)
    )
    assert narrow != wide
    assert "medium" in narrow
    assert "medium" in wide
    assert "dsv4-flash" in narrow
    assert "dsv4-flash" in wide
