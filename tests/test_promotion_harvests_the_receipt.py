"""Promotion preserves the client receipt as durable lane evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import rollout
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "receipt-project"
PLAN = "receipt-plan"
OBSERVED_AT = "2030-01-02T03:04:05Z"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path / "sessions")

    root = tmp_path / "repo"
    state_dir = root / "docs" / "state" / PROJECT
    state_dir.mkdir(parents=True)
    plan_path = root / "docs" / "plans" / f"{PLAN}.html"
    plan_path.parent.mkdir(parents=True)
    plan = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    plan_path.write_text(
        _plan_html.write_state(
            plan,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Receipt plan",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    return root


def _write_receipt(
    session_root: Path,
    session_id: str,
    *,
    context_window: int,
    windows: list[tuple[int, int, int]],
) -> None:
    directory = session_root / "2030" / "01" / "02"
    directory.mkdir(parents=True, exist_ok=True)
    quota_names = ("primary", "secondary")
    rate_limits = {
        name: {
            "window_minutes": window_minutes,
            "used_percent": used_percent,
            "resets_at": resets_at,
        }
        for name, (window_minutes, used_percent, resets_at) in zip(
            quota_names[: len(windows)], windows, strict=True
        )
    }
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "output_tokens": 1,
                },
                "last_token_usage": {"input_tokens": 8, "output_tokens": 1},
                "model_context_window": context_window,
            },
            "rate_limits": rate_limits,
        },
    }
    path = directory / f"rollout-2030-01-02T00-00-00-{session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _write_pointer(
    repository: Path,
    run_id: str,
    session_id: str,
    *,
    backend: str,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "backend": backend,
            "session_id": session_id,
            "created_at": "2030-01-02T03:00:00Z",
            "node": {
                "id": f"receipt-{run_id}",
                "plan": PLAN,
                "section": "receipt",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


def _promote(
    repository: Path,
    run_id: str,
    session_id: str,
    *,
    backend: str,
) -> dict:
    _write_pointer(repository, run_id, session_id, backend=backend)
    return crew.complete(
        run_id,
        gate="passed",
        completed_at=OBSERVED_AT,
        root=repository,
    )


def _stored_row(repository: Path, run_id: str) -> dict:
    ledger_path = repository / "docs" / "state" / PROJECT / "crew.json"
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    rows = [row for row in data["data"]["runs"] if row["run_id"] == run_id]
    assert len(rows) == 1
    return rows[0]


def _windows_by_length(receipt: dict) -> dict[int, dict]:
    return {row["window_minutes"]: row for row in receipt["quota_windows"]}


def test_every_quota_horizon_reaches_the_result_and_the_ledger(
    repository: Path,
) -> None:
    assert "lane_receipt" in ledger.RECORD_FIELDS
    session_id = "two-horizons"
    short_window = 5 * 60
    weekly_window = 7 * 24 * 60
    short_used = 73
    weekly_used = 41
    context_window = 121_600
    _write_receipt(
        rollout.CLIENT_SESSIONS_DIR,
        session_id,
        context_window=context_window,
        windows=[
            (short_window, short_used, 1_900_000_300),
            (weekly_window, weekly_used, 1_900_010_080),
        ],
    )

    result = _promote(
        repository,
        "r-two-horizons",
        session_id,
        backend="metered",
    )
    stored = _stored_row(repository, "r-two-horizons")

    returned = result["lane_receipt"]
    assert returned == result["record"]["lane_receipt"]
    assert stored["lane_receipt"] == returned
    assert stored is not result["record"]
    assert returned["effective_context_window"] == context_window
    windows = _windows_by_length(returned)
    assert set(windows) == {short_window, weekly_window}
    assert windows[short_window]["used_percent"] == short_used
    assert windows[weekly_window]["used_percent"] == weekly_used
    assert windows[weekly_window]["used_percent"] != short_used
    assert windows[short_window]["resets_at"] == 1_900_000_300
    assert windows[weekly_window]["resets_at"] == 1_900_010_080
    assert all(row["observed_at"] == OBSERVED_AT for row in windows.values())


def test_unmetered_is_not_the_same_reading_as_measured_zero(
    repository: Path,
) -> None:
    zero_session = "measured-zero"
    unmetered_session = "unmetered-zero"
    window = 5 * 60
    receipt_root = rollout.CLIENT_SESSIONS_DIR
    for session_id in (zero_session, unmetered_session):
        _write_receipt(
            receipt_root,
            session_id,
            context_window=121_600,
            windows=[(window, 0, 1_900_000_300)],
        )

    measured = _promote(
        repository,
        "r-measured-zero",
        zero_session,
        backend="metered",
    )["lane_receipt"]
    unmetered = _promote(
        repository,
        "r-unmetered-zero",
        unmetered_session,
        backend="clive",
    )["lane_receipt"]

    assert measured["quota_state"] == "measured"
    assert _windows_by_length(measured)[window]["used_percent"] == 0
    assert unmetered["quota_state"] == "unmeasured"
    assert unmetered["quota_windows"] == []
    assert unmetered["unmeasured"]["quota_windows"] == "unmetered"
    assert unmetered != measured


def test_an_absent_receipt_is_unmeasured_and_promotion_still_succeeds(
    repository: Path,
) -> None:
    result = _promote(
        repository,
        "r-missing-receipt",
        "no-such-receipt",
        backend="metered",
    )
    stored = _stored_row(repository, "r-missing-receipt")
    receipt = result["lane_receipt"]

    assert result["pointer_removed"] is True
    assert result["record"]["gate"] == "passed"
    assert stored["gate"] == "passed"
    assert stored["lane_receipt"] == receipt
    assert receipt["quota_state"] == "unmeasured"
    assert receipt["quota_windows"] == []
    assert receipt["effective_context_window"] == "unmeasured"
    assert receipt["unmeasured"]["quota_windows"] == "missing_rollout"
    assert receipt["unmeasured"]["effective_context_window"] == "missing_rollout"
    assert receipt["observed_at"] == OBSERVED_AT


# ── The measured model span reaches the committed run record ─────────────


def _write_span_rollout(
    session_root: Path,
    session_id: str,
    *,
    context_window: int,
) -> Path:
    """A rollout whose bounded tool spans measure the model's generation time.

    Wall spans 01Z to 11Z; the two bounded tool calls charge 4s and 3s, so the
    receipt measures machine 7.0s and generation 3.0s — the tuple a codex exec
    stream cannot report, which is what makes the join below load-bearing.
    """
    directory = session_root / "2030" / "01" / "02"
    directory.mkdir(parents=True, exist_ok=True)

    def tool_call(call_id: str, timestamp: str) -> dict[str, Any]:
        return {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "custom_tool_call",
                "call_id": call_id,
                "name": "probe",
            },
        }

    def tool_output(call_id: str, timestamp: str) -> dict[str, Any]:
        return {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": call_id,
                "output": [{"type": "input_text", "text": "done"}],
            },
        }

    records = [
        tool_call("bound-a", "2030-01-02T00:00:01.000Z"),
        tool_output("bound-a", "2030-01-02T00:00:05.000Z"),
        tool_call("bound-b", "2030-01-02T00:00:07.000Z"),
        tool_output("bound-b", "2030-01-02T00:00:10.000Z"),
        {
            "type": "event_msg",
            "timestamp": "2030-01-02T00:00:11.000Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 200,
                        "output_tokens": 100,
                    },
                    "last_token_usage": {"input_tokens": 900, "output_tokens": 100},
                    "model_context_window": context_window,
                },
            },
        },
    ]
    path = directory / f"rollout-2030-01-02T00-00-00-{session_id}.jsonl"
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    return path


def _codex_stream(repository: Path, run_id: str) -> Path:
    """A terminal codex exec stream that reports tokens but no span.

    The completed turn carries no duration fields, so the codex dialect leaves
    generation and machine time unmeasured: the rollout is the only authority
    that can rate the run, which is the join being tested.
    """
    stream = repository / "runs" / run_id / "stream.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"thread_id": f"thread-{run_id}", "type": "thread.started"},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 500, "output_tokens": 100},
        },
    ]
    stream.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8"
    )
    return stream


def _promote_codex(
    repository: Path, run_id: str, session_id: str, stream: Path
) -> dict:
    """Promote a spawned codex run the shape dispatch leaves behind."""
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "cli",
            "role": "implement",
            "backend": "codex",
            "session_id": session_id,
            "argv": ["codex", "exec", "--json"],
            "log_path": str(stream),
            "created_at": "2030-01-02T03:00:00Z",
            "node": {
                "id": f"receipt-{run_id}",
                "plan": PLAN,
                "section": "receipt",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )
    return crew.complete(
        run_id,
        gate="passed",
        completed_at=OBSERVED_AT,
        root=repository,
    )


def test_promoted_codex_rows_carry_the_rollout_span_across_the_cohort(
    repository: Path,
) -> None:
    """Every promoted codex run with a surviving rollout stores the measured span.

    The coverage count is the point, not a single fixture: each row in the
    promoted cohort must carry non-null generation and machine seconds and the
    derived rate, where at the base revision the promotion passed no receipt and
    stored none of them.
    """
    sessions = (
        ("r-span-prime", "span-prime", 121_600),
        ("r-span-second", "span-second", 121_600),
        ("r-span-third", "span-third", 121_600),
    )
    for run_id, session_id, context_window in sessions:
        _write_span_rollout(
            rollout.CLIENT_SESSIONS_DIR,
            session_id,
            context_window=context_window,
        )
        _promote_codex(
            repository, run_id, session_id, _codex_stream(repository, run_id)
        )

    measured_rows = 0
    for run_id, _session_id, _context_window in sessions:
        stored = _stored_row(repository, run_id)
        throughput = stored.get("throughput") or {}
        assert stored["gate"] == "passed"
        assert throughput.get("generation_seconds") is not None
        assert throughput.get("machine_seconds") is not None
        assert throughput.get("tokens_per_second") is not None
        # measured, and never a substitute zero
        assert throughput.get("generation_seconds") != 0
        assert throughput.get("tokens_per_second") == round(100 / 3, 2)
        measured_rows += 1
    assert measured_rows == len(sessions)


def test_a_codex_run_without_a_surviving_rollout_promotes_the_span_unmeasured(
    repository: Path,
) -> None:
    """An absent rollout is the marker, never a zero, and promotion still lands.

    The exec stream reports tokens but cannot rate them, so a run whose rollout
    did not survive keeps the span explicitly unmeasured on the row; the receipt
    read returns the marker object naming the missing rollout rather than a null
    or a zero standing in for a figure nobody measured.
    """
    run_id = "r-no-rollout"
    session_id = "no-rollout-session"
    _promote_codex(repository, run_id, session_id, _codex_stream(repository, run_id))

    stored = _stored_row(repository, run_id)
    throughput = stored.get("throughput") or {}
    assert stored["gate"] == "passed"
    assert throughput.get("generation_seconds") is None
    assert throughput.get("machine_seconds") is None
    assert throughput.get("tokens_per_second") is None
    assert throughput.get("generation_seconds") != 0

    missing = rollout.read_rollout_receipt(session_id)
    assert missing.generation_seconds is rollout.Unmeasured.MISSING_ROLLOUT
    assert missing.machine_seconds is rollout.Unmeasured.MISSING_ROLLOUT
    assert missing.generation_seconds is not None
    assert missing.generation_seconds != 0
