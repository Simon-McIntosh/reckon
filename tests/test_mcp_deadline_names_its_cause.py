"""A deadline that names whether the abandoned body was computing or waiting.

The MCP read deadline abandons a tool body at 30 s and reports a typed
``storage-slow`` result. Before this, the label claimed the filesystem was slow
with no evidence: a body that was busy computing read the same as one blocked
on the mount. These cases pin the two apart by measuring the worker thread's
CPU time and run-queue wait beside the wall time, while the ``error`` value
stays ``storage-slow`` so no existing caller's branch changes.

Two halves of "this body wanted the CPU" are measured, because either alone
misreads a loaded node. A body doing work burns CPU; a body starved runnable on
an oversubscribed node burns little CPU but sits on the runqueue, which is the
case that would otherwise read as waiting.

The spinning case is the negative control's subject: with the CPU measurement
replaced by zero, a computing body reports ``waiting`` and this test fails.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import reckon.mcp as mcp_module
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


def _run(body, label: str) -> dict:
    return asyncio.run(
        _run_under_deadline(body, kind="read", label=label, deadline=_DEADLINE_SECONDS)
    )


def test_spinning_body_reports_computing_with_wall_and_cpu_seconds() -> None:
    result = _run(lambda: _spin_for(_BODY_SECONDS), label="roadmap")

    assert result["cause"] == "computing"
    # The error value is unchanged so an existing caller's branch is unaffected.
    assert result["error"] == "storage-slow"
    assert result["waited_seconds"] >= _DEADLINE_SECONDS
    assert result["cpu_seconds"] is not None
    assert result["cpu_seconds"] >= 0.05
    assert result["run_seconds"] is not None
    assert "computing" in result["message"]
    # A mapped label carries the exact CLI command that answers that read
    # without the deadline, both in the field and in the hint.
    assert result["cli_command"] == "reckon roadmap --project <project>"
    assert "reckon roadmap --project <project>" in result["hint"]


def test_sleeping_body_reports_waiting_with_wall_and_cpu_seconds() -> None:
    result = _run(lambda: time.sleep(_BODY_SECONDS), label="read_plan")

    assert result["cause"] == "waiting"
    assert result["error"] == "storage-slow"
    assert result["waited_seconds"] >= _DEADLINE_SECONDS
    assert result["cpu_seconds"] is not None
    assert result["cpu_seconds"] < 0.05
    assert "waiting" in result["message"]
    # read_plan has no CLI counterpart, so no command is named: a hint that
    # pointed at `reckon roadmap` would answer a different read.
    assert result["cli_command"] is None
    assert "from the CLI" not in result["hint"]


def test_starved_runnable_body_reports_computing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body runnable but not running is computing, not waiting on storage.

    The body sleeps, so it burns almost no CPU: on CPU time alone it reads as
    waiting. Substituting the run-queue reader for the rise a starved body
    would show proves the run-queue half carries the verdict, and that the
    figure is reported.
    """

    readings = iter([0.0, 0.25])

    def _starved(_tid: int) -> float:
        return next(readings)

    monkeypatch.setattr(mcp_module, "_thread_run_wait_seconds", _starved)

    result = _run(lambda: time.sleep(_BODY_SECONDS), label="read_plan")

    assert result["cause"] == "computing"
    assert result["cpu_seconds"] is not None
    assert result["cpu_seconds"] < 0.05
    assert result["run_seconds"] == 0.25


@pytest.mark.parametrize(
    ("cpu_seconds", "run_seconds", "waited", "expected"),
    [
        # Busy on the CPU: computing.
        (0.20, 0.0, 0.20, "computing"),
        # Starved runnable, no CPU: still computing.
        (0.01, 0.19, 0.20, "computing"),
        # Blocked: no CPU and no run-queue time: waiting.
        (0.01, 0.0, 0.20, "waiting"),
        # Neither counter readable: unknown, reported as waiting.
        (None, None, 0.20, "waiting"),
    ],
)
def test_cause_combines_cpu_and_run_queue_time(
    cpu_seconds: float | None,
    run_seconds: float | None,
    waited: float,
    expected: str,
) -> None:
    assert mcp_module._cause_of_timeout(cpu_seconds, run_seconds, waited) == expected
