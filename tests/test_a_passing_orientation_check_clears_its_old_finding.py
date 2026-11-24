"""A passing orientation check retires the scope finding an earlier check wrote.

When a run committed a path outside its declared scope, the orientation check
wrote that finding into the run's detail. Widening the run's scope made the
next check pass, but the finding was removed only while the run's phase was
still ``blocked``; a run whose manifest had already restored its phase kept the
old text, and every later sweep read it as a live refusal. On 24 November three
widened runs raised sweep failures this way for over an hour.
"""

from __future__ import annotations

import json
from typing import Any

from reckon.crew import dispatch_sessions

SCOPE_FINDING = "committed path outside declared scope: reckon/module.py"


def _record(phase: str, detail: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "worktree": "/nonexistent/worktree",
        "base_sha": "0" * 40,
        "node": {"write_paths": ["reckon/module.py", "tests/test_module.py"]},
        "phase": phase,
    }
    if detail is not None:
        record["detail"] = detail
    return record


def _matching_echo(record: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "orientation_worktree": record["worktree"],
        "orientation_base_sha": record["base_sha"],
        "orientation_write_paths": json.dumps(record["node"]["write_paths"]),
        "status": status,
    }


def test_a_complete_run_loses_a_stale_scope_finding() -> None:
    record = _record("complete", SCOPE_FINDING)
    dispatch_sessions._apply_orientation_check(
        record, None, reported=_matching_echo(record, "blocked")
    )
    assert record["orientation_check"]["matched"] is True
    assert "detail" not in record, record.get("detail")
    assert record["phase"] == "complete"


def test_a_blocked_run_takes_its_status_when_the_finding_clears() -> None:
    record = _record("blocked", "orientation mismatch: write_paths expected=[]")
    dispatch_sessions._apply_orientation_check(
        record, None, reported=_matching_echo(record, "blocked")
    )
    assert "detail" not in record
    assert record["phase"] == "blocked"


def test_a_detail_from_another_stage_is_kept() -> None:
    other = "process exited without a terminal event in its log; check stderr"
    record = _record("orphaned", other)
    dispatch_sessions._apply_orientation_check(
        record, None, reported=_matching_echo(record, "blocked")
    )
    assert record["detail"] == other
    assert record["phase"] == "orphaned"
