"""A slow read names what it measured, not just that it was slow.

A timed-out MCP read used to carry one label, ``storage-slow``, whatever the
deadline had caught. A read that spent 13.1 s of CPU and 1.4 s runnable out of
a 30 s deadline is a read the node could not run fast enough; the label sent
its reader to blame the mount, and the stat, vmstat and wait-channel readings
taken at the same moment said the mount was answering.

The deadline already measured the three figures. These cases pin them to the
verdict: a body at 13 s CPU and 1.4 s runnable of 30 s reports ``read-slow``
and carries all three, and a body blocked with no CPU reports
``storage-slow``. Both are stubbed thread counters, so the verdict cannot move
with the machine's load.

Two surfaces read the error value — the transcript census and the doctor
printer — and both count the two values in one total, because both say the
call missed its deadline.

The negative control restores the half-wall threshold and the fixed label: the
computing case then reports ``storage-slow`` and this file fails.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from reckon import mcp_census, mcp_deadlines
from reckon.mcp_deadlines import _run_under_deadline
from reckon.project_maintenance_commands import _echo_mcp_census

# The figures the deadline reported for the read this node is about: a 30 s
# deadline, of which the abandoned body held 13.1 s of CPU and 1.4 s runnable.
_WALL_SECONDS = 30.0
_CPU_SECONDS = 13.1
_RUNNABLE_SECONDS = 1.4


class _Readings:
    """A thread counter that answers a fixed sequence, then repeats its last."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self, _tid: int) -> float:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


class _Clock:
    """A monotonic clock that starts at a reading and jumps when the wait ends.

    The deadline's wall figure is read from this clock, so the case can hold a
    30 s wait without spending 30 s. The event loop keeps its own clock, which
    is why the wait itself is stubbed beside it.
    """

    def __init__(self, start: float, *rest: float) -> None:
        self._values = [start, *rest]
        self._index = 0

    def monotonic(self) -> float:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


async def _expire_now(_awaitable: object, _limit: float) -> object:
    """Stand in for the wait so the deadline expires without the wait being real."""

    raise TimeoutError


def _timed_out_body(
    monkeypatch: pytest.MonkeyPatch, *, cpu: float, runnable: float
) -> dict:
    """Run one read body to its stubbed deadline and return the result.

    The body finishes at once: what the case measures is the verdict the
    deadline reaches on the counters it is handed, and a body that outlives a
    real 30 s wait would make the case a wall-clock test of the machine.
    """

    monkeypatch.setattr(mcp_deadlines, "time", _Clock(1000.0, 1000.0 + _WALL_SECONDS))
    monkeypatch.setattr(asyncio, "wait_for", _expire_now)
    monkeypatch.setattr(mcp_deadlines, "_thread_cpu_seconds", _Readings([0.0, cpu]))
    monkeypatch.setattr(
        mcp_deadlines, "_thread_run_wait_seconds", _Readings([0.0, runnable])
    )
    return asyncio.run(
        _run_under_deadline(
            lambda: None, kind="read", label="crew", deadline=_WALL_SECONDS
        )
    )


def test_computing_read_reports_read_slow_with_its_three_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _timed_out_body(monkeypatch, cpu=_CPU_SECONDS, runnable=_RUNNABLE_SECONDS)

    assert result["error"] == "read-slow"
    assert result["cause"] == "computing"
    assert result["cpu_seconds"] == _CPU_SECONDS
    assert result["run_seconds"] == _RUNNABLE_SECONDS
    assert result["waited_seconds"] == _WALL_SECONDS
    assert result["deadline_seconds"] == _WALL_SECONDS
    # The three figures are in the message a caller reads, beside the cause.
    assert "13.1s CPU" in result["message"]
    assert "1.4s runnable" in result["message"]
    assert "waited 30.0s" in result["message"]
    # The storage is not the suspect, so the hint must not send a reader there
    # as the only explanation.
    assert "computing" in result["hint"]


def test_blocked_body_reports_storage_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _timed_out_body(monkeypatch, cpu=0.0, runnable=0.0)

    assert result["error"] == "storage-slow"
    assert result["cause"] == "waiting"
    assert "waiting on storage" in result["message"]
    # A blocked body keeps the storage hint: here the mount really is the
    # suspect, and the read-slow hint would send the reader somewhere else.
    assert "Storage is slow" in result["hint"]
    assert "still computing" not in result["hint"]


def _transcript_tree(root) -> object:
    """Write one transcript holding a read-slow and a storage-slow result."""

    project = root / "proj-a"
    project.mkdir(parents=True)

    def tool_use(uid: str, name: str) -> dict:
        return {
            "type": "assistant",
            "isSidechain": False,
            "timestamp": "2026-10-03T12:00:00.000Z",
            "message": {"content": [{"type": "tool_use", "id": uid, "name": name}]},
        }

    def tool_result(uid: str, payload: dict) -> dict:
        return {
            "type": "user",
            "isSidechain": False,
            "timestamp": "2026-10-03T12:00:01.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": uid,
                        "content": json.dumps(payload),
                    }
                ]
            },
        }

    records = [
        tool_use("u1", "mcp__reckon__read_plan"),
        tool_result("u1", {"ok": False, "error": "read-slow", "label": "read_plan"}),
        tool_use("u2", "mcp__reckon__roadmap"),
        tool_result("u2", {"ok": False, "error": "storage-slow", "label": "roadmap"}),
        # A result that only mentions the phrase in prose is not counted, so the
        # reader is shown to discriminate rather than match any occurrence.
        tool_use("u3", "mcp__reckon__read_plan"),
        tool_result(
            "u3", {"ok": True, "label": "read_plan", "note": "not storage-slow"}
        ),
    ]
    (project / "session1.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n"
    )
    return root


def test_census_counts_a_read_slow_result_beside_a_storage_slow_one(
    tmp_path,
) -> None:
    transcript_root = _transcript_tree(tmp_path / "transcripts")

    counts = mcp_census._storage_slow(
        transcript_root, start=0.0, end=time.time() + 3600.0
    )

    # The instrument sees what is known present: the storage-slow result a
    # reader already counted still counts, and the read-slow result joins it.
    assert counts == {"read_plan": 1, "roadmap": 1}


def test_the_printer_shows_a_read_slow_count(tmp_path, capsys) -> None:
    transcript_root = _transcript_tree(tmp_path / "transcripts")
    counts = mcp_census._storage_slow(
        transcript_root, start=0.0, end=time.time() + 3600.0
    )

    _echo_mcp_census({"servers": {}, "storage_slow": counts})

    printed = capsys.readouterr().out
    assert "read_plan 1" in printed
    assert "roadmap 1" in printed


def test_both_errors_are_the_same_slow_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The census pattern matches either error and no prose mention of them."""

    assert mcp_census._SLOW_ERROR.search('{"error": "read-slow"}')
    assert mcp_census._SLOW_ERROR.search('{"error": "storage-slow"}')
    assert not mcp_census._SLOW_ERROR.search(
        '{"note": "a result that was not storage-slow either"}'
    )
