"""A deadline that names whether the abandoned body was computing or waiting.

The MCP read deadline abandons a tool body at 30 s and reports a typed
``storage-slow`` result. Before this, the label claimed the filesystem was slow
with no evidence: a body that was busy computing read the same as one blocked
on the mount. These cases pin the two apart by measuring the worker thread's
CPU time beside the wall time, while the ``error`` value stays ``storage-slow``
so no existing caller's branch changes.

The spinning case is the negative control's subject: with the CPU measurement
replaced by zero, a computing body reports ``waiting`` and this test fails.
"""

from __future__ import annotations

import asyncio
import time

from reckon.mcp import _run_under_deadline

# A deadline short enough to keep the case quick, with a body that outlives it
# by a wide margin so the reading is stable whatever the machine's load.
_DEADLINE_SECONDS = 0.25
_BODY_SECONDS = 0.8


def _spin_for(seconds: float) -> None:
    end = time.monotonic() + seconds
    total = 0
    while time.monotonic() < end:
        total += 1


def _run(body) -> dict:
    return asyncio.run(
        _run_under_deadline(
            body, kind="read", label="read_plan", deadline=_DEADLINE_SECONDS
        )
    )


def test_spinning_body_reports_computing_with_wall_and_cpu_seconds() -> None:
    result = _run(lambda: _spin_for(_BODY_SECONDS))

    assert result["cause"] == "computing"
    # The error value is unchanged so an existing caller's branch is unaffected.
    assert result["error"] == "storage-slow"
    assert result["waited_seconds"] >= _DEADLINE_SECONDS
    assert result["cpu_seconds"] is not None
    assert result["cpu_seconds"] >= 0.05
    assert "computing" in result["message"]
    # The hint must name the cause and give the CLI command that answers
    # without a deadline, rather than sending the reader to retry storage.
    assert "computing" in result["hint"]
    assert "reckon" in result["hint"]


def test_sleeping_body_reports_waiting_with_wall_and_cpu_seconds() -> None:
    result = _run(lambda: time.sleep(_BODY_SECONDS))

    assert result["cause"] == "waiting"
    assert result["error"] == "storage-slow"
    assert result["waited_seconds"] >= _DEADLINE_SECONDS
    assert result["cpu_seconds"] is not None
    assert result["cpu_seconds"] < 0.05
    assert "waiting" in result["message"]
