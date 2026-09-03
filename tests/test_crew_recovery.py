"""Hermetic recovery and project-watch stream contracts."""

from __future__ import annotations

import importlib
import json
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
from reckon.crew import recovery, reports

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


def test_terminal_manifest_never_counts_as_working(home) -> None:
    # A manifest that has reached a verdict is a finished run no matter what the
    # record phase claims, so none of the terminal readings may land in the
    # working bucket.
    for status in ("complete", "blocked", "failed"):
        snapshot = _snapshot_pointer(
            home,
            f"r-term-{status}",
            phase="working",
            alive=True,
            manifest_status=status,
        )
        assert snapshot["state"] not in recovery.FLEET_WORKING_STATES, (
            f"a terminal manifest ({status!r}) read as {snapshot['state']!r}, "
            "which counts as working"
        )


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
            home, "r-done", phase="working", alive=True, manifest_status="complete"
        ),
    }
    counts = recovery._fleet_counts(snapshots)
    assert counts == {"working": 1, "blocked": 1, "unpromoted": 1}
    assert sum(counts.values()) == len(snapshots)


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


def test_refusal_blocked_pointer_snapshots_as_blocked_in_the_ticker_path(home) -> None:
    pointer = _cli_pointer(home, "r-refused", "codex-usage-limit.jsonl")
    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] == "blocked"
    assert snapshot["state"] in recovery.FLEET_BLOCKED_STATES
    assert "usage-limit" in snapshot["reason"]
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
    assert "the schema rejects an enum value" in row["detail"]
    assert row["detail"].strip() != "|"

    snapshot = recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] == "blocked"
    assert snapshot["marker"] == "?"
    assert (
        snapshot["reason"] == "the schema rejects an enum value the config file needs"
    )

    transition = recovery._watch_transition(
        "proj",
        kind="baseline",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({"r-asked": snapshot}),
    )
    assert transition["marker"] == "?"
    assert (
        transition["reason"] == "the schema rejects an enum value the config file needs"
    )


def test_blockers_without_a_needs_help_report_get_the_blocker_text_and_a_bang_marker(
    home,
) -> None:
    record = _blocked_record(home, "r-blocked", manifest_text=_BLOCKER_ONLY_BLOCK)

    row = recovery.classify_pointer(record, now_seconds=time.time())

    assert row["classification"] == "blocked"
    assert row["marker"] == "!"
    assert "the credential file is missing" in row["detail"]

    snapshot = recovery._watch_snapshot(record, moment=time.time(), stall_seconds=3600)
    assert snapshot["state"] == "blocked"
    assert snapshot["marker"] == "!"
    assert snapshot["reason"] == (
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
    assert snapshot["reason"] != "|"


@pytest.mark.parametrize("bare", ["|", ">", '"', "'", "|-", ">+"])
def test_single_clause_refuses_a_bare_punctuation_reason(bare) -> None:
    assert recovery._single_clause(bare) == ""


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
    assert "implement" in line


def test_the_role_stamped_on_the_node_reaches_the_transition_tool(home) -> None:
    # Dispatch writes the role both on the record root and on the node; both
    # spellings are the same fact, so both must thread.
    transition = _role_transition(home, "r-node-role", node_role="test")

    assert transition["role"] == "test"


def test_documentation_is_narrowed_to_docs_by_the_renderer(home) -> None:
    transition = _role_transition(home, "r-doc", role="documentation")

    assert transition["role"] == "documentation"
    line = recovery.format_watch_transition(transition)
    assert "docs" in line


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
# carried on every transition it is given.
_TICKER_READ_FIELDS = (
    "observed_at",
    "role",
    "node",
    "run_id",
    "session",
    "to_state",
    "from_state",
    "agent",
    "reason",
    "working",
    "blocked",
    "unpromoted",
)


def test_snapshot_carries_every_field_the_ticker_column_set_reads(home) -> None:
    # Constructed from a pointer whose manifest is blocked so the reason clause
    # is populated; a snapshot whose state supplied nothing to explain would not
    # exercise the reason slot the renderer reads. The aliased agent keeps the
    # presence check honest: a field that is present but reduces stale passes a
    # presence-only assertion, so each field must also equal the reduction the
    # renderer expects — here, the alias rather than the model the record also
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
    assert transition["agent"] == "dsv4-flash·me"
    line = recovery.format_watch_transition(transition)
    assert "dsv4-flash" in line
    assert "deepseek-v4-flash" not in line
    assert "implement" in line


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
    # the render, which is the path the measured bug dropped it on.
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
    assert transition["agent"] == "dsv4-flash·me"
    line = recovery.format_watch_transition(transition)
    assert "dsv4-flash" in line
    assert "deepseek-v4-flash" not in line
