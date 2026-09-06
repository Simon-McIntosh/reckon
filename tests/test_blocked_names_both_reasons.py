"""A blocked row names the refusal that gates its recovery, not only the
reason its worker last wrote.

A run whose manifest says ``blocked`` while its lane also refused a turn
carries two facts of different kinds. The manifest reason is what the worker
was doing when it stopped; the provider refusal is when anything can be
attempted at all, and it names a reset. When both are present the manifest arm
outranks the refusal arm, so a row read ``blocked: foreground H200 job ...``
with no hint that the account window was exhausted five days out — a
coordinator attempted the resume, refused on budget, and learned the reset by
attempting it.

The fix is not to reorder the arms: a NEEDS-HELP question on a spent lane
still needs its answer, so the classification, the manifest reason and the
marker all stay. The refusal is added to the detail with its reset, phrased so
the reader can tell which must clear first, and the offered next action is
gated on the lane clearing rather than proposing a resume the budget gate
would refuse.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from reckon.crew import recovery

_USAGE_REFUSAL = {
    "headroom": "known",
    "utilisation_pct": 100.0,
    "rate_limit_type": "usage-limit",
    "resets_at": "2026-09-11T12:00:00+00:00",
    "threshold_status": "exhausted",
    "refusal": True,
    "detail": "backend refused the turn: the account's usage-limit is reached",
}

_SPEND_REFUSAL = {
    "headroom": "known",
    "utilisation_pct": 100.0,
    "rate_limit_type": "spend-limit",
    "resets_at": "2026-09-11T12:00:00+00:00",
    "threshold_status": "exhausted",
    "refusal": True,
    "detail": "backend refused the turn: the account's spend-limit is reached",
}

_H200_BLOCK = """\
node: r-h200
status: blocked
blockers: |
  the foreground H200 job is still running and the node cannot submit further
"""

_NEEDS_HELP_BLOCK = """\
node: r-asked
status: blocked
blockers: |
  NEEDS-HELP: which fixture variant should the shipped check assert?
  tried: set the literal; the choices were rejected
  options: keep the literal; or derive it from the fixture
  leaning: derive it, because the literal ages
  cost-if-wrong: the generated schema and its committed JSON regenerate
"""


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    phase: str = "blocked",
    alive: bool = False,
    manifest: str | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A cli pointer with an optional manifest and an optional refusal budget.

    The refusal is folded onto the pointer as the budget block, exactly as
    observe() folds a stream's reading, so the classifier reaches the same
    refusal either way. A folded ``session_id`` lets the blocked run resolve
    its session from the pointer instead of consulting the stream or the
    promoted ledger, keeping the test inside the code under test.
    """
    manifest_path = tmp_path / "manifests" / f"{run_id}.md"
    if manifest is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest)
    stderr = tmp_path / f"{run_id}.stderr.log"
    stderr.write_text("")
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "30m"},
        "backend": "codex",
        "launch": "cli",
        "argv": ["codex"],
        "log_path": str(tmp_path / "streams" / f"{run_id}.jsonl"),
        "stderr_path": str(stderr),
        "manifest_path": str(manifest_path),
        "phase": phase,
        "process_alive": alive,
        "session": "s",
        "session_id": "sess-1",
    }
    if budget is not None:
        record["budget"] = budget
    return record


def _classify(pointer: dict[str, Any]) -> dict[str, Any]:
    return recovery.classify_pointer(pointer, now_seconds=time.time())


# ── The combined case reports both, and names the gate ─────────────────────


def test_a_blocked_manifest_with_a_spent_lane_names_the_refusal_as_the_gate(
    tmp_path,
) -> None:
    pointer = _pointer(tmp_path, "r-h200", manifest=_H200_BLOCK, budget=_USAGE_REFUSAL)

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    # The manifest reason is kept, not replaced.
    assert row["detail"].startswith("the worker manifest reports blocked:")
    assert "foreground H200 job" in row["detail"]
    # The refusal is named too, with its backend, limit kind and reset.
    assert "usage-limit" in row["detail"]
    assert "codex" in row["detail"]
    assert "reset 2026-09-11T12:00:00+00:00" in row["detail"]
    # The reader can tell which must clear first.
    assert "must clear first" in row["detail"]
    assert "no resume may be attempted before it does" in row["detail"]
    # The action is gated on the lane clearing, not proposed as do-able now.
    assert row["next_action"].endswith("once the lane clears")
    assert row["marker"] == "!"


def test_a_blocked_needs_help_question_stays_answerable_on_a_spent_lane(
    tmp_path,
) -> None:
    pointer = _pointer(
        tmp_path, "r-asked", manifest=_NEEDS_HELP_BLOCK, budget=_SPEND_REFUSAL
    )

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert row["marker"] == "?"
    assert row["needs_help_complete"] is True
    # A spent lane does not make the question moot.
    assert "which fixture variant should the shipped check assert" in row["detail"]
    # The gate is named beside it.
    assert "spend-limit" in row["detail"]
    assert "must clear first" in row["detail"]
    assert "--advice" in row["next_action"]
    assert row["next_action"].endswith("once the lane clears")


def test_the_monitor_row_for_the_combined_case_also_names_the_refusal(
    tmp_path,
) -> None:
    pointer = _pointer(tmp_path, "r-h200", manifest=_H200_BLOCK, budget=_USAGE_REFUSAL)

    snapshot = recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)

    assert snapshot["state"] == "blocked"
    # The ticker strips the manifest's own prefix but keeps the refusal clause,
    # so the row a coordinator watches names the same gate the detail does.
    assert "foreground H200 job" in snapshot["detail"]
    assert "usage-limit" in snapshot["detail"]
    assert "must clear first" in snapshot["detail"]


# ── The falsifiers: what has one fact only reads exactly as it did ─────────


def test_a_blocked_manifest_without_a_refusal_reads_exactly_as_today(
    tmp_path,
) -> None:
    pointer = _pointer(tmp_path, "r-only", manifest=_H200_BLOCK)

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert row["detail"] == (
        "the worker manifest reports blocked: the foreground H200 job is still "
        "running and the node cannot submit further"
    )
    assert row["marker"] == "!"
    assert row["next_action"] == (
        f"read {pointer['manifest_path']}; resolve the blocker before resuming the run"
    )
    assert "once the lane clears" not in row["next_action"]


def test_a_refusal_without_a_manifest_reason_reads_exactly_as_today(
    tmp_path,
) -> None:
    pointer = _pointer(tmp_path, "r-refused", manifest=None, budget=_USAGE_REFUSAL)

    row = _classify(pointer)

    assert row["classification"] == "blocked"
    assert row["detail"] == (
        "blocked: backend 'codex' refused the turn on a usage-limit; reset "
        "2026-09-11T12:00:00+00:00; no manifest was delivered and nothing has "
        "landed yet"
    )
    assert row["next_action"] == (
        "reckon crew resume --run r-refused once the limit lifts"
    )
    assert "the worker manifest reports blocked" not in row["detail"]
    assert "must clear first" not in row["detail"]


def test_a_run_with_neither_a_manifest_reason_nor_a_refusal_reads_as_today(
    tmp_path,
) -> None:
    pointer = _pointer(tmp_path, "r-neither", phase="working", manifest=None)

    row = _classify(pointer)

    assert row["classification"] == "abandoned"
    assert row["detail"] == (
        "the process is gone without a complete manifest; nothing is eligible "
        "for promotion"
    )
    assert "must clear first" not in row["detail"]
