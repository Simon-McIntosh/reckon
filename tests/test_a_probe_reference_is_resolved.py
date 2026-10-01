"""A declared wait probe's reference is resolved, not inferred from characters.

A probe that cannot fail is not a probe: ``echo pending`` prints a constant and
``git rev-parse HEAD`` reports the worker's own tree, so a wait resting on one
is satisfied on every sweep however the awaited work is doing. The reading that
refuses them looked for a digit, a slash, a dollar sign or a backtick anywhere
in the probe's text, so ``git rev-parse HEAD2`` — a constant with a digit in it
— read as a reference to something outside the worker, and a wait resting on it
tested nothing.

The reading here resolves the reference instead: a token counts only when its
place in the vector names it as a job id, a pid, a port or a path, and the
answer carries the kind it found. Each case asserts the kind beside the
verdict, because the kind is what separates this reading from the one it
replaces.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from reckon.crew import recovery


@pytest.mark.parametrize(
    "probe",
    [
        ["git", "rev-parse", "HEAD2"],
        ["git", "rev-parse", "--short", "HEAD2"],
        ["git", "show-ref", "--verify", "HEAD2"],
    ],
    ids=("rev-parse", "rev-parse-short", "show-ref"),
)
def test_a_constant_token_carrying_a_digit_names_no_reference(probe) -> None:
    """The specimen the character-class reading accepted: a digit is not one.

    ``HEAD2`` names no process, no job the scheduler knows, no port and no
    path, so nothing about it can differ between two sweeps however the
    awaited work is doing — the whole reason the probe is refused.
    """
    assert recovery._wait_probe_reference_kind(probe) is None
    assert recovery._wait_probe_cannot_fail(probe) is True


def test_a_live_pid_is_resolved_as_a_pid_reference() -> None:
    """The test's own pid: a process that is demonstrably there."""
    probe = ["kill", "-0", str(os.getpid())]

    assert recovery._wait_probe_reference_kind(probe) == "pid"
    assert recovery._wait_probe_cannot_fail(probe) is False


def test_a_pid_given_to_a_process_command_is_resolved_as_a_pid_reference() -> None:
    probe = ["ps", "-p", str(os.getpid())]

    assert recovery._wait_probe_reference_kind(probe) == "pid"
    assert recovery._wait_probe_cannot_fail(probe) is False


def test_an_existing_path_is_resolved_as_a_path_reference(tmp_path: Path) -> None:
    """A path the case creates: the reference resolves while it is there."""
    state = tmp_path / "scheduler-state.txt"
    state.write_text("PENDING\n", encoding="utf-8")
    probe = ["cat", str(state)]

    assert recovery._wait_probe_reference_kind(probe) == "path"
    assert recovery._wait_probe_cannot_fail(probe) is False


def test_a_path_that_is_not_there_resolves_to_no_reference(tmp_path: Path) -> None:
    """The other direction of the same resolution, so it cannot pass on shape.

    A path reference is resolved on the filesystem, so a token that is shaped
    like one and points at nothing names no kind either. A path a wait is *for*
    is declared through the file-test form, whose operand is read as the path
    being waited on and is not required to exist yet.
    """
    probe = ["cat", str(tmp_path / "never-written.txt")]

    assert recovery._wait_probe_reference_kind(probe) is None
    assert recovery._wait_probe_cannot_fail(probe) is True


def test_a_file_test_operand_is_the_path_being_waited_for(tmp_path: Path) -> None:
    """The file-test form names a path that does not exist yet, and is read."""
    probe = ["test", "-f", str(tmp_path / "not-yet-written.txt")]

    assert recovery._wait_probe_reference_kind(probe) == "path"
    assert recovery._wait_probe_cannot_fail(probe) is False


def test_a_port_with_a_port_flag_is_resolved_as_a_port_reference() -> None:
    probe = ["ssh", "-p", "2222", "probe.invalid"]

    assert recovery._wait_probe_reference_kind(probe) == "port"
    assert recovery._wait_probe_cannot_fail(probe) is False


def test_a_port_given_by_position_is_resolved_as_a_port_reference() -> None:
    probe = ["nc", "-z", "a-host", "8765"]

    assert recovery._wait_probe_reference_kind(probe) == "port"
    assert recovery._wait_probe_cannot_fail(probe) is False


@pytest.mark.parametrize(
    "probe",
    [
        ["squeue", "-h", "-j", "1271081"],
        ["scheduler-status", "--job", "7788"],
    ],
    ids=("short-flag", "long-flag"),
)
def test_a_job_id_with_a_job_flag_is_resolved_as_a_job_reference(probe) -> None:
    """Short and long spellings of the same reference both resolve."""
    assert recovery._wait_probe_reference_kind(probe) == "job"
    assert recovery._wait_probe_cannot_fail(probe) is False


def test_a_shell_argument_is_read_as_the_vector_it_is() -> None:
    """A shell keeps its program in one token, and the rule reads through it.

    This is the shape the fleet actually declares when a probe needs a
    scheduler query or a file test inside one string; the job id is a real
    reference there even though the digits sit in the middle of the string.
    """
    probe = [
        "bash",
        "-lc",
        'q=$(squeue -h -j 1274028); if [ -n "$q" ]; then echo RUNNING; fi',
    ]

    assert recovery._wait_probe_reference_kind(probe) == "job"
    assert recovery._wait_probe_cannot_fail(probe) is False


def test_the_reader_refuses_the_constant_probe_it_once_accepted(
    tmp_path: Path,
) -> None:
    """The reading reaches the sweep that lifts a park, not only the unit.

    A constant probe is satisfied unconditionally, so a sweep resuming on one
    would end a wait nothing had ended. The declaration therefore has to come
    back from the reader as something to repair, and it is the refusal message
    that carries the repair.
    """
    directory = tmp_path / "run"
    directory.mkdir()
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                "node: probe-reference-fixture",
                "status: waiting",
                "wait_condition: the scheduler job has left the queue",
                f"wait_probe: {json.dumps(['git', 'rev-parse', 'HEAD2'])}",
                f"wait_terminal: {json.dumps(['exit:0'])}",
                "resume_brief: collect the scheduler result and finish",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    stream = directory / "stream.jsonl"
    stream.write_text('{"type":"assistant","text":"working"}\n', encoding="utf-8")
    record = {
        "run_id": "r-probe-reference-fixture",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "worktree": str(directory),
    }

    wait = recovery.external_wait(record, now_seconds=time.time() + 1)

    assert wait is not None
    assert wait["valid"] is False
    assert "cannot fail" in wait["error"]
    assert "HEAD2" in wait["error"]
