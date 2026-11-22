"""A gate exit status is read from its leading integer, and refused by name.

A worker records a gate's status the way its job printed it, as the number
followed by where the number came from — ``0 (job 1335599, all_debug)``. The
number is the status: refusing the whole value leaves the gate unrecorded, and
a promotion then reads the run as having no passing gate to re-run. A value
that does not open with an integer cannot be a status at all, so it is refused
naming the field and the text rather than silently dropped.
"""

from __future__ import annotations

import click
import pytest

from reckon import crew_run_commands as commands

LEADING_STATUS = "0 (job 1335599, all_debug)"
FAILING_STATUS = "1 (two failures)"


def _gate_exit_status_option(command):
    """The ``--gate-exit-status`` option every promotion verb shares."""
    return next(
        option for option in command.params if option.name == "gate_exit_status"
    )


def _read_option(command, text):
    """Read ``text`` through the command's own option, as the CLI does."""
    option = _gate_exit_status_option(command)
    return option.type.convert(text, option, None)


def test_a_leading_integer_is_read_as_a_passing_gate() -> None:
    """``0 (job …)`` is the passing status 0, not a value to refuse."""
    assert _read_option(commands.crew_complete, LEADING_STATUS) == 0
    assert _read_option(commands.crew_merge, LEADING_STATUS) == 0
    # The reader that decides whether a recorded gate passed sees the same
    # integer, so a stored row carrying the provenance is a passing gate.
    assert commands._gate_exit_status({"exit_status": LEADING_STATUS}) == 0


def test_a_leading_integer_is_read_as_a_failing_gate() -> None:
    """``1 (two failures)`` is the failing status 1, not a value to refuse."""
    assert _read_option(commands.crew_complete, FAILING_STATUS) == 1
    assert _read_option(commands.crew_merge, FAILING_STATUS) == 1
    assert commands._gate_exit_status({"exit_status": FAILING_STATUS}) == 1


def test_a_status_without_a_leading_integer_is_refused_by_name() -> None:
    """``passed`` names no status, so the refusal names the field and the text."""
    with pytest.raises(click.UsageError) as refused:
        _read_option(commands.crew_complete, "passed")
    message = str(refused.value)
    assert "gate_exit_status" in message
    assert "passed" in message