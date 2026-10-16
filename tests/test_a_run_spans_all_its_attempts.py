"""A promoted run's timing spans every attempt it made.

A run that failed over to another lane or resumed many times keeps one
timestamped stream — the first attempt — beside streams that carry no event
timestamps at all. Reading the run's end and its worker span from whichever
stream happens to carry timestamps therefore records a morning of work as its
opening two minutes: the primary run in the plan ran for eleven hours and was
recorded as 76 s, with 18 s of worker time, from the two minutes its first
claude attempt spent before it moved lane to a codex client.

The tests below make the multi-attempt thing happen — a timestamped first
stream ending at T1 beside two codex streams ending at T2 and T3 — and read the
promoted row's figures, rather than resting on a suite that happens to be green.
Single-attempt claude and codex runs must keep recording what they recorded
before, so both shapes are pinned here too.

The fixture writes only inside a temporary crew home and a temporary
repository; the client rollout receipts are read from the run's own harness
home, never from the operator's ``~/.codex``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion_checks
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"

REVIEW_WAIVER = "fixture: the run's timing is the subject, not repository work"

# Dispatch and the three attempt ends, as instants. The last attempt's end is
# the run's end under the change; the first attempt's is what the unchanged
# reading mistook for it.
DISPATCH = "2026-10-15T16:07:00Z"
FIRST_ATTEMPT_OPEN = "2026-10-15T16:08:06Z"
FIRST_ATTEMPT_END = "2026-10-15T16:08:25Z"
SECOND_ATTEMPT_END = "2026-10-15T17:00:00Z"
THIRD_ATTEMPT_END = "2026-10-15T18:00:00Z"

FIRST_ATTEMPT_SPAN_SECONDS = 19
# The two codex clients' measured spans: generation plus machine, read from
# their rollout receipts.
FIRST_CODEX_SPAN_SECONDS = 120
SECOND_CODEX_SPAN_SECONDS = 230
MULTI_ATTEMPT_WORKER_SECONDS = (
    FIRST_ATTEMPT_SPAN_SECONDS
    + FIRST_CODEX_SPAN_SECONDS
    + SECOND_CODEX_SPAN_SECONDS
)
MULTI_ATTEMPT_WALL_SECONDS = 6780  # 16:07:00Z to 18:00:00Z

FIRST_CODEX_SESSION = "aaaaaaaa-1111-2222-3333-444444444444"
SECOND_CODEX_SESSION = "bbbbbbbb-1111-2222-3333-444444444444"


def _epoch(moment: str) -> float:
    return datetime.fromisoformat(moment.replace("Z", "+00:00")).timestamp()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "comments": {},
        },
    )
    # Promotion commits the two tracked stores it writes, so a fixture that
    # promotes must be a git worktree with a committed head to land into.
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


# ── fixture run directories ─────────────────────────────────────────────────


def _write_claude_stream(path: Path, *, opened: str, closed: str) -> None:
    """One timestamped claude attempt, spanning ``opened`` to ``closed``."""
    records = [
        {"type": "system", "subtype": "init", "session_id": "claude-session"},
        {"type": "assistant", "timestamp": opened, "message": {"content": []}},
        {"type": "assistant", "timestamp": closed, "message": {"content": []}},
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _write_codex_stream(path: Path, session_id: str) -> None:
    """One codex attempt that carries no event timestamps, as codex writes one."""
    records = [
        {"type": "thread.started", "thread_id": session_id},
        {
            "type": "item.completed",
            "item": {"id": "i0", "type": "agent_message", "text": "continued"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _touch(path: Path, moment: str) -> None:
    """Set a stream's mtime to the instant its attempt ended."""
    seconds = _epoch(moment)
    os.utime(path, (seconds, seconds))


def _write_rollout(
    run_id: str,
    session_id: str,
    *,
    opened: str,
    closed: str,
    tool_seconds: int,
) -> None:
    """A codex client rollout measuring one session's span.

    The bounded tool span is the machine wait; generation is wall minus that, so
    the receipt's two figures sum to the session's own wall span. A
    ``token_count`` record is what makes the receipt measurable at all.
    """
    root = run_dir(run_id) / "codex-home" / "sessions" / "2026" / "10" / "15"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-2026-10-15T16-20-00-{session_id}.jsonl"
    tool_start = _epoch(opened)
    records = [
        {
            "timestamp": datetime.fromtimestamp(tool_start, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "payload": {"type": "custom_tool_call", "call_id": "c1"},
        },
        {
            "timestamp": datetime.fromtimestamp(
                tool_start + tool_seconds, tz=timezone.utc
            )
            .isoformat()
            .replace("+00:00", "Z"),
            "payload": {"type": "custom_tool_call_output", "call_id": "c1"},
        },
        {
            "timestamp": closed,
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 100, "output_tokens": 20},
                    "last_token_usage": {"input_tokens": 100, "output_tokens": 20},
                },
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _write_pointer(
    repository: Path,
    run_id: str,
    manifest: Path,
    *,
    log_path: Path | None,
    backend: str,
    argv: list[str],
    session_id: str,
    created_at: str = DISPATCH,
) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: none\n", encoding="utf-8"
    )
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "cli",
        "role": "implement",
        "member": "worker-a",
        "backend": backend,
        "argv": argv,
        "created_at": created_at,
        "manifest_path": str(manifest),
        "session_id": session_id,
    }
    if log_path is not None:
        record["log_path"] = str(log_path)
    record["node"] = {
        "id": "node-a",
        "plan": PLAN,
        "section": "§2",
        "time_budget": "35m",
        "write_paths": [],
    }
    _write_json(pointer_path(run_id), record)


def _gate_check(log: Path) -> dict:
    log.write_text("the check ran\nEXIT=0\n", encoding="utf-8")
    return {
        "command": "true",
        "exit_status": 0,
        "log_path": str(log),
        "log_digest": "",
    }


def _promote(repository: Path, run_id: str, tmp_path: Path) -> dict:
    crew.complete(
        run_id,
        gate="passed",
        outcome="the attempt's timing landed",
        root=repository,
        gate_check=_gate_check(tmp_path / f"{run_id}-gate.log"),
        review_waiver=REVIEW_WAIVER,
    )
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id") or "") == run_id),
        None,
    )
    assert row is not None, "the promotion did not commit a ledger row"
    return dict(row)


# ── the multi-attempt run ───────────────────────────────────────────────────


def test_a_run_moved_lane_and_resumed_records_its_last_attempts_end(
    repository: Path, tmp_path: Path
) -> None:
    """The end and the span come from every attempt, not the first stream."""
    run_id = "r-20261015T160700000000-node-a"
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    first = directory / "stream.jsonl"
    _write_claude_stream(first, opened=FIRST_ATTEMPT_OPEN, closed=FIRST_ATTEMPT_END)
    _touch(first, FIRST_ATTEMPT_END)

    later_first = directory / "resume-1.jsonl"
    _write_codex_stream(later_first, FIRST_CODEX_SESSION)
    _touch(later_first, SECOND_ATTEMPT_END)

    later_second = directory / "resume-2.jsonl"
    _write_codex_stream(later_second, SECOND_CODEX_SESSION)
    _touch(later_second, THIRD_ATTEMPT_END)

    _write_rollout(
        run_id,
        FIRST_CODEX_SESSION,
        opened="2026-10-15T16:20:00Z",
        closed="2026-10-15T16:22:00Z",
        tool_seconds=20,
    )
    _write_rollout(
        run_id,
        SECOND_CODEX_SESSION,
        opened="2026-10-15T16:30:00Z",
        closed="2026-10-15T16:33:50Z",
        tool_seconds=30,
    )

    _write_pointer(
        repository,
        run_id,
        tmp_path / "manifests" / f"{run_id}.md",
        log_path=first,
        backend="codex",
        argv=["codex", "exec", "--json"],
        session_id=SECOND_CODEX_SESSION,
    )

    row = _promote(repository, run_id, tmp_path)

    assert row["completed_at"] == THIRD_ATTEMPT_END
    assert row["completed_at_source"] == "stream_mtime"
    assert row["wall_seconds"] == MULTI_ATTEMPT_WALL_SECONDS
    assert row["worker_seconds"] == MULTI_ATTEMPT_WORKER_SECONDS
    assert row["worker_seconds_source"] == "stream_events+rollout_receipts"


def test_an_attempt_that_shared_a_session_is_counted_once(
    repository: Path, tmp_path: Path
) -> None:
    """Two streams of one codex session is one client, not two spans."""
    run_id = "r-20261015T160700000001-node-a"
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    first = directory / "stream.jsonl"
    _write_claude_stream(first, opened=FIRST_ATTEMPT_OPEN, closed=FIRST_ATTEMPT_END)
    _touch(first, FIRST_ATTEMPT_END)

    for name, ended in (
        ("resume-1.jsonl", SECOND_ATTEMPT_END),
        ("resume-2.jsonl", THIRD_ATTEMPT_END),
    ):
        stream = directory / name
        _write_codex_stream(stream, FIRST_CODEX_SESSION)
        _touch(stream, ended)

    _write_rollout(
        run_id,
        FIRST_CODEX_SESSION,
        opened="2026-10-15T16:20:00Z",
        closed="2026-10-15T16:22:00Z",
        tool_seconds=20,
    )

    _write_pointer(
        repository,
        run_id,
        tmp_path / "manifests" / f"{run_id}.md",
        log_path=first,
        backend="codex",
        argv=["codex", "exec", "--json"],
        session_id=FIRST_CODEX_SESSION,
    )

    row = _promote(repository, run_id, tmp_path)

    assert row["completed_at"] == THIRD_ATTEMPT_END
    assert row["worker_seconds"] == FIRST_ATTEMPT_SPAN_SECONDS + FIRST_CODEX_SPAN_SECONDS
    assert row["worker_seconds_source"] == "stream_events+rollout_receipts"


def test_a_codex_attempts_session_is_credited_from_its_observation(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session credited to a codex attempt is observe_log's own reading.

    A codex attempt's span is found through the session its stream names, so the
    credit must carry the session observe_log already read from that stream —
    its ``thread_id`` as ``session_id``. The record's own session_id is set to a
    different value with no receipt, so a credit taken from the record rather
    than the stream would lose the receipt's span.
    """
    run_id = "r-20261015T160700000004-node-a"
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    first = directory / "stream.jsonl"
    _write_claude_stream(first, opened=FIRST_ATTEMPT_OPEN, closed=FIRST_ATTEMPT_END)
    _touch(first, FIRST_ATTEMPT_END)

    resumed = directory / "resume-1.jsonl"
    _write_codex_stream(resumed, FIRST_CODEX_SESSION)
    _touch(resumed, SECOND_ATTEMPT_END)

    _write_rollout(
        run_id,
        FIRST_CODEX_SESSION,
        opened="2026-10-15T16:20:00Z",
        closed="2026-10-15T16:22:00Z",
        tool_seconds=20,
    )

    _write_pointer(
        repository,
        run_id,
        tmp_path / "manifests" / f"{run_id}.md",
        log_path=first,
        backend="codex",
        argv=["codex", "exec", "--json"],
        session_id=SECOND_CODEX_SESSION,
    )

    observed: list[str | None] = []
    credited: list[str | None] = []
    real_observe = promotion_checks._backends.observe_log
    real_receipt = promotion_checks._measured_rollout_receipt

    def spy_observe(*args, **kwargs):
        observation = real_observe(*args, **kwargs)
        observed.append(observation.session_id)
        return observation

    def spy_receipt(session_id, run_id):
        credited.append(session_id)
        return real_receipt(session_id, run_id)

    monkeypatch.setattr(promotion_checks._backends, "observe_log", spy_observe)
    monkeypatch.setattr(promotion_checks, "_measured_rollout_receipt", spy_receipt)

    row = _promote(repository, run_id, tmp_path)

    # observe_log read the codex attempt's session from its stream, and that same
    # session is the one the attempt's span was credited through.
    assert FIRST_CODEX_SESSION in observed
    assert FIRST_CODEX_SESSION in credited
    assert row["worker_seconds"] == (
        FIRST_ATTEMPT_SPAN_SECONDS + FIRST_CODEX_SPAN_SECONDS
    )


# ── the single-attempt shapes must not move ─────────────────────────────────


def test_a_single_attempt_claude_run_records_its_own_span(
    repository: Path, tmp_path: Path
) -> None:
    """One timestamped attempt records exactly what it recorded before."""
    run_id = "r-20261015T160700000002-node-a"
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    stream = directory / "stream.jsonl"
    _write_claude_stream(stream, opened=FIRST_ATTEMPT_OPEN, closed=FIRST_ATTEMPT_END)
    _touch(stream, FIRST_ATTEMPT_END)

    _write_pointer(
        repository,
        run_id,
        tmp_path / "manifests" / f"{run_id}.md",
        log_path=stream,
        backend="claude",
        argv=["claude", "-p"],
        session_id="",
    )

    row = _promote(repository, run_id, tmp_path)

    assert row["completed_at"] == FIRST_ATTEMPT_END
    assert row["completed_at_source"] == "terminal_event"
    assert row["worker_seconds"] == FIRST_ATTEMPT_SPAN_SECONDS
    assert row["worker_seconds_source"] == "stream_events"


def test_a_single_attempt_codex_run_records_its_receipt(
    repository: Path, tmp_path: Path
) -> None:
    """One codex attempt records the mtime end and its receipt's span."""
    run_id = "r-20261015T160700000003-node-a"
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    stream = directory / "stream.jsonl"
    _write_codex_stream(stream, FIRST_CODEX_SESSION)
    _touch(stream, FIRST_ATTEMPT_END)

    _write_rollout(
        run_id,
        FIRST_CODEX_SESSION,
        opened="2026-10-15T16:20:00Z",
        closed="2026-10-15T16:22:00Z",
        tool_seconds=20,
    )

    _write_pointer(
        repository,
        run_id,
        tmp_path / "manifests" / f"{run_id}.md",
        log_path=stream,
        backend="codex",
        argv=["codex", "exec", "--json"],
        session_id=FIRST_CODEX_SESSION,
    )

    row = _promote(repository, run_id, tmp_path)

    assert row["completed_at"] == FIRST_ATTEMPT_END
    assert row["completed_at_source"] == "stream_mtime"
    assert row["worker_seconds"] == FIRST_CODEX_SPAN_SECONDS
    assert row["worker_seconds_source"] == "rollout_receipt"