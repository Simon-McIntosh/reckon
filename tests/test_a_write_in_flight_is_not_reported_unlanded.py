"""A timed-out write still in flight is reported unknown, not not-landed.

The MCP write deadline abandons a tool body that outlives it and reports a
typed ``storage-slow`` result carrying a ``landed`` verdict. Reporting the
abandoned write as ``landed=False`` before its body had finished is a measured
defect: twice on 2026-10-15 an ``edit_plan`` write timed out, read
``landed=false``, and had actually landed about 20 s later. These cases drive
the deadline with three bodies and pin the landing verdict to the abandoned
body's own future:

* a body that writes its file only after the landing grace is still running
  when the result is composed, so ``landed`` is ``None`` with wording and a
  hint that say to read the resource back rather than retry;
* a body that finishes without writing its file reports ``landed=False``;
* a body that writes within the grace reports ``landed=True``.

The landing check reads ``_InflightBody.future`` for the first case: a body
still running at the end of the grace cannot have settled its write, so the
honest answer is unknown rather than false. The negative control replaces that
with the earlier behaviour — answer ``False`` whenever the grace ends with the
file unchanged — and the first case then reports ``landed=False`` and fails.

No real plan or crew home is touched: each body writes a temporary file.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from reckon._mcp_tools import _INFLIGHT_WRITE_HINT, _STORAGE_SLOW_HINT
from reckon.mcp import _run_under_deadline
from reckon.mcp_deadlines import LANDING_GRACE_ENV

# A deadline short enough to keep the cases quick, with bodies that outlive it
# by a wide margin so the reading is stable whatever the machine's load.
_DEADLINE_SECONDS = 0.1
_GRACE_SECONDS = 0.4


def _await_file(target: Path, limit: float = 2.0) -> str:
    """Wait for the abandoned body's write to appear, then return its content.

    The abandoned thread finishes after the result was composed, so the file is
    polled rather than read immediately; a bounded wait so the case cannot hang.
    """

    stop = time.monotonic() + limit
    while time.monotonic() < stop:
        if target.exists():
            return target.read_text()
        time.sleep(0.02)
    raise AssertionError(f"{target} was never written")


def _run_write(body, target: Path) -> dict:
    return asyncio.run(
        _run_under_deadline(
            body,
            kind="write",
            label="edit_plan",
            path=str(target),
            deadline=_DEADLINE_SECONDS,
        )
    )


def test_write_still_in_flight_reports_landed_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that writes only after the grace is in flight, not failed."""

    target = tmp_path / "plan.html"
    monkeypatch.setenv(LANDING_GRACE_ENV, str(_GRACE_SECONDS))

    def body() -> None:
        time.sleep(_DEADLINE_SECONDS + _GRACE_SECONDS + 0.3)
        target.write_text("landed late")

    result = _run_write(body, target)

    assert result["landed"] is None
    assert result["error"] == "storage-slow"
    assert "still in flight" in result["message"]
    assert result["hint"] == _INFLIGHT_WRITE_HINT
    assert "read the resource back" in result["hint"].lower()
    assert "resubmit only what is missing" in result["hint"].lower()
    # The retry hint would invite a blind retry, so it is not used.
    assert result["hint"] != _STORAGE_SLOW_HINT
    assert "retry once the storage recovers" not in result["hint"].lower()
    # The abandoned body really did land, so ``None`` was the honest answer and
    # ``False`` — what the old check reported — would have been wrong.
    assert _await_file(target) == "landed late"


def test_write_finishing_without_writing_reports_landed_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that has finished and left the file unchanged did not land."""

    target = tmp_path / "plan.html"
    monkeypatch.setenv(LANDING_GRACE_ENV, str(_GRACE_SECONDS))

    def body() -> None:
        # Outlives the deadline so the call is abandoned, but returns well
        # inside the grace without touching the file.
        time.sleep(_DEADLINE_SECONDS + 0.05)

    result = _run_write(body, target)

    assert result["landed"] is False
    assert not target.exists()
    # A write that genuinely did not land keeps the retry hint.
    assert result["hint"] == _STORAGE_SLOW_HINT


def test_write_within_the_grace_reports_landed_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that writes within the grace lands and is reported so."""

    target = tmp_path / "plan.html"
    monkeypatch.setenv(LANDING_GRACE_ENV, "1.0")

    def body() -> None:
        time.sleep(_DEADLINE_SECONDS + 0.1)
        target.write_text("landed in time")

    result = _run_write(body, target)

    assert result["landed"] is True
    assert target.read_text() == "landed in time"
    assert "do not resubmit" in result["message"]
