"""Census-of-inherited-context behaviour over a synthesised runs directory.

The census reads real crew stream grammars, so the fixtures are minimal streams
in those same grammars: a claude-code stream (``system``, ``assistant``,
``result``) and a codex stream (``thread.started``, ``turn.completed``).  Only
the fields the census consumes are present, which also pins the contract that it reports
a resumed run's first-turn inheritance, a fresh run's zero inheritance, and an
unmeasurable codex first turn rather than a contaminated sum.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew.carryover_census import carryover_census

FRESH = "r-20260101T000000000001-fresh-node"
RESUMED = "r-20260101T000001000002-resumed-node"
CODEX = "r-20260101T000002000003-codex-node"

SESSION = "session-shared"


def _write_stream(runs: Path, run_id: str, records: list[dict]) -> None:
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.txt").write_text("x" * 400, encoding="utf-8")
    with (run_dir / "stream.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _claude_stream(session: str, first_input: int, first_cache_read: int) -> list[dict]:
    return [
        {"type": "system", "session_id": session, "model": "claude-x"},
        {
            "type": "assistant",
            "message": {
                "id": "msg-one",
                "usage": {
                    "input_tokens": first_input,
                    "cache_read_input_tokens": first_cache_read,
                },
            },
        },
        {
            "type": "result",
            "modelUsage": {
                "claude-x": {
                    "inputTokens": 999_999,
                    "contextWindow": 200_000,
                }
            },
        },
    ]


def _build_runs(root: Path) -> Path:
    # The resumed run shares the fresh run's session and is dispatched later, so
    # its first turn carries what the fresh run left in the session.
    _write_stream(root, FRESH, _claude_stream(SESSION, 1_000, 500))
    _write_stream(root, RESUMED, _claude_stream(SESSION, 5_000, 10_000))
    _write_stream(
        root,
        CODEX,
        [
            {"type": "thread.started", "thread_id": "thread-fresh"},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 777_777, "cached_input_tokens": 111_111},
            },
        ],
    )
    return root


def _rows(report: dict) -> dict[str, dict]:
    return {row["run_id"]: row for row in report["runs"]}


def test_classifies_fresh_and_resumed_and_codex(tmp_path: Path) -> None:
    runs = _build_runs(tmp_path / "runs")
    report = carryover_census(
        runs,
        recorded_backends={FRESH: "clive", RESUMED: "clive", CODEX: "codex"},
        recorded_members={FRESH: "member-a", RESUMED: "member-a"},
    )
    rows = _rows(report)

    fresh = rows[FRESH]
    assert fresh["resumed"] is False
    assert fresh["prior_run"] is None
    assert fresh["first_turn_input_tokens"] == 1_500
    assert fresh["first_turn_basis"] == "first-assistant-turn-usage"
    # A run that opened its session carried nothing in: the positive control.
    assert fresh["inherited_tokens"] == 0
    assert fresh["inherited_over_own_prompt"] is False

    resumed = rows[RESUMED]
    assert resumed["resumed"] is True
    assert resumed["prior_run"] == FRESH
    assert resumed["first_turn_input_tokens"] == 15_000
    # 15_000 read in minus the 1_500 fresh baseline = 13_500 inherited.
    assert resumed["inherited_tokens"] == 13_500
    assert resumed["inherited_over_own_prompt"] is True
    assert resumed["backend"] == "clive"
    assert resumed["backend_basis"] == "recorded"
    assert resumed["member"] == "member-a"

    codex = rows[CODEX]
    assert codex["first_turn_input_tokens"] is None
    assert codex["first_turn_basis"] == "unavailable-in-dialect"
    assert codex["turn_cumulative_input_tokens"] == 777_777
    # It opened its own session, so it carried nothing in — reported zero
    # regardless of the codex dialect not publishing a first-turn figure.
    assert codex["inherited_tokens"] == 0
    assert codex["inherited_over_own_prompt"] is False


def test_per_backend_aggregation_counts_resumed_inheritance(tmp_path: Path) -> None:
    runs = _build_runs(tmp_path / "runs")
    report = carryover_census(
        runs,
        recorded_backends={FRESH: "clive", RESUMED: "clive", CODEX: "codex"},
    )

    clive = report["per_backend"]["clive"]
    assert clive["runs"] == 2
    assert clive["resumed_runs"] == 1
    assert clive["resumed_inherited_over_own_prompt"] == 1
    assert clive["median_inherited_tokens"] == 13_500
    assert clive["max_inherited_tokens"] == 13_500

    codex = report["per_backend"]["codex"]
    assert codex["runs"] == 1
    assert codex["resumed_runs"] == 0
    assert codex["median_inherited_tokens"] is None
    assert codex["max_inherited_tokens"] is None


def test_examples_state_both_dialects_with_cumulative(tmp_path: Path) -> None:
    runs = _build_runs(tmp_path / "runs")
    report = carryover_census(runs)

    examples = report["examples"]
    assert examples["claude"]["run_id"] in {FRESH, RESUMED}
    assert examples["claude"]["first_turn_input_tokens"] is not None
    assert examples["codex"]["run_id"] == CODEX
    assert examples["codex"]["first_turn_input_tokens"] is None
    assert examples["codex"]["turn_cumulative_input_tokens"] == 777_777

    notes = report["dialect_notes"]
    assert CODEX in notes["codex"]
    assert "777777" in notes["codex"]
    assert "claude-code grammar" in notes["clive"]
