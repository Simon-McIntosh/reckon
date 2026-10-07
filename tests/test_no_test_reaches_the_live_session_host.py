"""A test never reaches the session host of the Claude session that ran it.

A suite run from a live Claude Code session inherits its CLAUDE_PID, and
dispatch names that session's host FIFO from it, so a test dispatch could write
a request the live host acts on. The conftest fixture removes the pid for every
test; these check that the removal holds and that dispatch then names no FIFO.
"""

from __future__ import annotations

import os

from reckon.crew.dispatch import _session_host_fifo


def test_no_test_sees_the_live_claude_pid() -> None:
    assert "CLAUDE_PID" not in os.environ


def test_dispatch_names_no_session_host_fifo_without_a_claude_pid() -> None:
    assert _session_host_fifo() is None
