"""Recovery state says why a run stopped and what the reader should do."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption
from reckon.crew.ticker import Ticker


def _pointer(tmp_path: Path, run_id: str, *, alive: bool = False) -> dict:
    return {
        "run_id": run_id,
        "project": "fixture-project",
        "process_alive": alive,
        "phase": "working",
        "manifest_path": str(tmp_path / f"{run_id}.md"),
        "log_path": str(tmp_path / f"{run_id}.jsonl"),
        "stderr_path": str(tmp_path / f"{run_id}.stderr"),
        "worktree": str(tmp_path / f"{run_id}-worktree"),
        "backend": "fixture-lane",
        "node": {"id": run_id, "role": "implement", "time_budget": "20m"},
    }


@pytest.fixture(autouse=True)
def _no_external_session_reads(monkeypatch) -> None:
    monkeypatch.setattr(
        recovery,
        "_blocked_session_resolution",
        lambda _record, _run_id: {
            "resolved": True,
            "session_id": "fixture-session",
            "source": "fixture",
        },
    )


def _manifest(pointer: dict, text: str) -> Path:
    path = Path(pointer["manifest_path"])
    path.write_text(text, encoding="utf-8")
    return path


def _typed_examples(tmp_path: Path, monkeypatch) -> dict[str, dict]:
    held_pointer = _pointer(tmp_path, "held")
    original_stream_budget = recovery._stream_budget

    def stream_budget(record):
        if record["run_id"] == "held":
            return {
                "refusal": True,
                "rate_limit_type": "spend-limit",
                "resets_at": "2026-09-08T10:00:00Z",
            }
        return original_stream_budget(record)

    monkeypatch.setattr(recovery, "_stream_budget", stream_budget)

    answer_pointer = _pointer(tmp_path, "answer")
    _manifest(
        answer_pointer,
        "NEEDS-HELP: choose the compatible schema\n"
        "tried: both declared encodings\n"
        "options: keep the stable encoding or migrate it\n"
        "leaning: keep the stable encoding\n"
        "cost-if-wrong: one parser must be rewritten\n"
        "status: blocked",
    )

    failed_pointer = _pointer(tmp_path, "failed")
    _manifest(failed_pointer, "status: failed\nblockers: assertion failed\n")

    blocked_pointer = _pointer(tmp_path, "decision")
    _manifest(blocked_pointer, "status: blocked\nblockers: choose a storage policy\n")

    stalled_pointer = _pointer(tmp_path, "stalled", alive=True)
    Path(stalled_pointer["log_path"]).write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(recovery, "_stream_quiet_seconds", lambda *_a, **_k: 901)
    monkeypatch.setattr(recovery, "_stall_wait_reason", lambda _record: None)

    return {
        "held": recovery.classify_pointer(held_pointer),
        "needs-help": recovery.classify_pointer(answer_pointer),
        "failed": recovery.classify_pointer(failed_pointer),
        "stalled": recovery._watch_snapshot(
            stalled_pointer, moment=1_800_000_000.0, stall_seconds=900
        ),
        "blocked": recovery.classify_pointer(blocked_pointer),
    }


def test_causes_with_opposite_recoveries_have_distinct_types(
    tmp_path: Path, monkeypatch
) -> None:
    rows = _typed_examples(tmp_path, monkeypatch)

    assert {row["recovery_classification"] for row in rows.values()} == set(rows)
    assert rows["held"]["recovery"] == "resume"
    assert "resume" in rows["held"]["next_action"]
    assert rows["failed"]["recovery"] == "redispatch"
    assert "resume" not in rows["failed"]["next_action"]
    assert rows["needs-help"]["recovery"] == "answer"
    assert rows["stalled"]["recovery"] == "investigate"
    assert rows["blocked"]["recovery"] == "decide"


@pytest.mark.parametrize("reset", ["2026-09-08T10:00:00Z", None])
def test_a_lane_hold_always_states_its_reset(
    tmp_path: Path, monkeypatch, reset: str | None
) -> None:
    pointer = _pointer(tmp_path, "held-reset")
    monkeypatch.setattr(
        recovery,
        "_stream_budget",
        lambda _record: {
            "refusal": True,
            "rate_limit_type": "quota",
            "resets_at": reset,
        },
    )

    row = recovery.classify_pointer(pointer)

    assert row["recovery_classification"] == "held"
    assert row["resets_at"] == (reset or "unknown")
    assert str(row["resets_at"]) in row["detail"]
    snapshot = recovery._watch_snapshot(
        pointer, moment=1_800_000_000.0, stall_seconds=900
    )
    counts = recovery._fleet_counts({"held-reset": snapshot})
    event = recovery._watch_transition(
        "fixture-project",
        kind="transition",
        snapshot=snapshot,
        previous="working",
        current=str(snapshot["state"]),
        counts=counts,
    )
    line = Ticker(width=180).render(event)
    assert counts["blocked"] == 0
    assert counts["waiting"] == 1
    assert "held" in line
    assert "resume:" in line


def test_a_live_in_progress_manifest_is_not_a_block(
    tmp_path: Path, monkeypatch
) -> None:
    pointer = _pointer(tmp_path, "working", alive=True)
    Path(pointer["log_path"]).write_text("{}\n", encoding="utf-8")
    manifest = _manifest(pointer, "status: in-progress\ncommits: none\n")
    os.utime(manifest, (1_800_000_000.0, 1_800_000_000.0))

    row = recovery.classify_pointer(pointer, now_seconds=1_800_000_001.0)

    assert row["classification"] == "running"
    assert row["recovery_classification"] == "running"
    assert row["classification"] != "blocked"
    assert row["manifest_status"] == "in-progress"
    assert "in-progress" in recovery.NON_TERMINAL_MANIFEST_STATUSES


def test_an_unsubstituted_manifest_template_is_unwritten_and_non_terminal(
    tmp_path: Path,
) -> None:
    pointer = _pointer(tmp_path, "template")
    _manifest(pointer, "node: <node>\nstatus: complete | blocked | failed\n")

    row = recovery.classify_pointer(pointer)

    assert row["classification"] == "running"
    assert row["recovery_classification"] == "unwritten"
    assert row["manifest_status"] is None
    assert row["manifest_reported_status"] == "complete | blocked | failed"
    assert not recovery.manifest_status_is_terminal(row["manifest_reported_status"])


def test_wait_age_uses_the_declared_horizon(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.md"
    manifest.write_text("status: waiting\n", encoding="utf-8")
    os.utime(manifest, (1_700_000_000.0, 1_700_000_000.0))
    common = {
        "status": "waiting",
        "wait_condition": "scheduler job",
        "wait_probe": ["scheduler-status", "--job", "42"],
        "wait_terminal": ["COMPLETED", "FAILED"],
        "resume_brief": "collect the result",
        "wait_started_at": "2023-11-14T22:13:20+00:00",
    }

    short = recovery._manifest_wait(
        {**common, "wait_expected": "10m"},
        manifest,
        now_seconds=1_700_001_200.0,
        stale_after_seconds=900,
    )
    long = recovery._manifest_wait(
        {**common, "wait_expected": "59m"},
        manifest,
        now_seconds=1_700_001_200.0,
        stale_after_seconds=900,
    )

    assert short is not None and long is not None
    assert short["overdue"] is True
    assert long["overdue"] is False
    assert short["expected_horizon_seconds"] == 600
    assert long["expected_horizon_seconds"] == 3540


@pytest.mark.parametrize("alive", [True, False])
def test_a_declared_wait_outranks_worker_liveness(tmp_path: Path, alive: bool) -> None:
    pointer = _pointer(tmp_path, "parked", alive=alive)
    manifest = _manifest(
        pointer,
        "status: waiting\n"
        "wait_condition: scheduler job 42\n"
        'wait_probe: ["scheduler-status", "--job", "42"]\n'
        'wait_terminal: ["COMPLETED", "FAILED"]\n'
        "wait_started_at: 2026-09-08T07:42:00+00:00\n"
        "wait_expected: 59m\n"
        "resume_brief: collect the scheduler result\n",
    )
    log = Path(pointer["log_path"])
    log.write_text("{}\n", encoding="utf-8")
    os.utime(manifest, ns=(1_788_853_800_000_000_000,) * 2)
    os.utime(log, ns=(1_788_853_801_000_000_000,) * 2)

    row = recovery.classify_pointer(pointer, now_seconds=1_788_853_920.0)

    assert row["classification"] == recovery.WAITING_STATUS
    assert row["classification"] != "abandoned"
    assert row["recovery_classification"] == "waiting"
    assert row["lifting_condition"]


def test_resume_sweep_keeps_a_dead_declared_wait_in_its_collection(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    pointer = _pointer(tmp_path, "parked-for-sweep", alive=False)
    _manifest(
        pointer,
        "status: waiting\n"
        "wait_condition: scheduler job 42\n"
        'wait_probe: ["scheduler-status", "--job", "42"]\n'
        'wait_terminal: ["COMPLETED", "FAILED"]\n'
        "wait_started_at: 2026-09-08T07:42:00+00:00\n"
        "wait_expected: 59m\n"
        "resume_brief: collect the scheduler result\n",
    )
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [pointer])

    report = resumption.sweep(
        "fixture-project",
        dry_run=True,
        condition_test=lambda _pointer, _wait: False,
    )

    assert report["checked"] == 1
    assert [row["run_id"] for row in report["skipped"]] == ["parked-for-sweep"]
    assert report["skipped"][0]["reason"] == "condition-pending"


def test_a_malformed_declared_wait_is_unreadable_not_abandoned(
    tmp_path: Path,
) -> None:
    pointer = _pointer(tmp_path, "malformed-wait", alive=False)
    _manifest(
        pointer,
        "status: waiting\n"
        "wait_condition: scheduler job 42\n"
        'wait_probe: ["scheduler-status", "--job", "42"]\n'
        "wait_terminal: [COMPLETED, FAILED]\n"
        "resume_brief: collect the scheduler result\n",
    )

    row = recovery.classify_pointer(pointer)

    assert row["classification"] == "unreadable"
    assert row["classification"] != "abandoned"
    assert "wait_terminal" in str(row["manifest_error"])


def test_every_actionable_type_declares_one_recovery_verb() -> None:
    assert recovery.ACTIONABLE_RECOVERY_CLASSIFICATIONS
    assert set(recovery.RECOVERY_CLASSIFICATIONS) == set(recovery.RECOVERY_VERBS)
    assert all(
        recovery.RECOVERY_VERBS.get(classification)
        for classification in recovery.ACTIONABLE_RECOVERY_CLASSIFICATIONS
    )


def test_every_self_lifting_type_names_what_lifts_it() -> None:
    assert recovery.SELF_LIFTING_RECOVERY_CLASSIFICATIONS
    assert all(
        recovery.DEFAULT_LIFTING_CONDITIONS.get(classification)
        for classification in recovery.SELF_LIFTING_RECOVERY_CLASSIFICATIONS
    )


def test_ticker_carries_the_recovery_verb_in_an_actionable_row() -> None:
    line = Ticker(width=180).render(
        {
            "observed_at": "2026-09-08T08:00:00Z",
            "node": "fixture",
            "role": "implement",
            "from_state": "working",
            "to_state": "blocked",
            "working": 0,
            "blocked": 1,
            "unpromoted": 0,
            "recovery": "decide",
            "detail": "a storage policy is required",
            "needs_help_complete": False,
        }
    )

    assert "decide:" in line
