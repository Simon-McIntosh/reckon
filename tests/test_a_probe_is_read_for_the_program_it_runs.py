"""A probe is read for the program it actually runs.

A wait probe may reach the scheduler through a shell's string or through an
interpreter's code string. The reference check read only a shell's string, so a
the interpreter probe from the reported incident — a Python ``-c`` string
running ``squeue -h -j 1306915`` — was one opaque token to it and the
declaration was refused as a probe that cannot fail. That refusal was false:
the job id was in the argument, and the run sat idle.

The reader here answers which program a probe runs and with what words, so the
reference check and the structured-output check both look inside the code
string. A probe that still names no reference keeps its refusal, and now says
which interpreter it runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import recovery_liveness, recovery_wait

NOW = 1_761_500_000.0

# The probe reported in the incident, verbatim from the plan's §1. The middle
# of the code string is elided there exactly as it was written to the manifest,
# and the words the reader needs — the program, its flags and the job id — are
# all present.
SECTION_1_PROBE = [
    "python3",
    "-c",
    (
        "import subprocess; p=subprocess.run(['squeue','-h','-j','1306915'],...); "
        "print('PENDING' if p.stdout.strip() else 'TERMINAL')"
    ),
]

INTERPRETER_REFUSAL = ["python3", "-c", "print('pending')"]

STRUCTURED_INTERPRETER_PROBE = [
    "python3",
    "-c",
    "import subprocess; subprocess.run(['sacct','--json','-j','1306915'])",
]

GLUED_PROBE = ["squeue", "-h", "-j1306915"]

SHELL_PROBE = ["bash", "-c", "squeue -h -j 1274028); echo"]


def _declaration(probe: list[str]) -> dict:
    """The manifest wait the reader builds for a declaration carrying a probe."""
    return recovery_liveness._manifest_wait(
        {
            "status": "waiting",
            "wait_condition": "the scheduler job has left the queue",
            "wait_probe": json.dumps(probe),
            "wait_terminal": '["COMPLETED", "FAILED"]',
            "wait_started_at": "2026-10-13T16:05:00+00:00",
            "wait_expected": "3h",
            "resume_brief": "collect the scheduler result",
        },
        Path("/nonexistent/manifest.md"),
        now_seconds=NOW,
        stale_after_seconds=1800,
    )


def test_the_incident_probe_is_read_and_names_job_1306915() -> None:
    """The §1 probe, verbatim: accepted, and the job id it names is read out.

    Both halves matter. The reference kind is what stops the cannot-fail
    refusal, and the returned job id is what a scheduler liveness check asks
    about (§4 reads it). A reader that saw only the kind would leave the run
    unable to say which job keeps it waiting.
    """
    assert recovery_wait._wait_probe_reference_kind(SECTION_1_PROBE) == "job"
    assert recovery_wait._wait_probe_job_id(SECTION_1_PROBE) == "1306915"
    assert recovery_wait._wait_probe_cannot_fail(SECTION_1_PROBE) is False


def test_an_interpreter_probe_naming_no_reference_is_refused_by_name() -> None:
    """A constant code string is still refused, and the refusal names python3.

    The refusal the incident received said only that the probe named nothing
    outside the worker, which is false for a code string that does carry a
    reference and gives the worker nowhere to look. This one names the
    interpreter whose code string was read, so the repair is obvious.
    """
    declaration = _declaration(INTERPRETER_REFUSAL)

    assert declaration is not None
    assert declaration["valid"] is False
    assert (
        "runs the interpreter python3 whose code string names no reference"
        in declaration["error"]
    )
    assert recovery_wait._wait_probe_cannot_fail(INTERPRETER_REFUSAL) is True


def test_a_no_op_code_string_stays_a_visible_refusal() -> None:
    """``python3 -c true`` is refused, not silently read as no declaration.

    The head tests stay on the vector's own program for this reason: routing an
    interpreter through the reader would turn today's cannot-fail refusal into
    the silent drop that ``true`` already gets.
    """
    declaration = _declaration(["python3", "-c", "true"])

    assert declaration is not None
    assert declaration["valid"] is False
    assert "cannot fail" in declaration["error"]


def test_a_structured_query_inside_a_code_string_is_refused() -> None:
    """``sacct --json`` is caught inside an interpreter exactly as outside it.

    The structured-output check took the probe's first element, so a scheduler
    query reached through a code string was never read as one and its JSON last
    line could never equal a state token.
    """
    assert (
        recovery_liveness._wait_probe_prints_structured_output(
            STRUCTURED_INTERPRETER_PROBE
        )
        == "--json"
    )
    assert (
        recovery_liveness._wait_probe_prints_structured_output(
            ["sacct", "--json", "-j", "1306915"]
        )
        == "--json"
    )
    declaration = _declaration(STRUCTURED_INTERPRETER_PROBE)
    assert declaration is not None
    assert declaration["valid"] is False
    assert "sacct -n -X -o State -j" in declaration["error"]


def test_a_glued_job_flag_is_accepted_as_naming_its_job() -> None:
    """``-j1306915`` names the same job as ``-j 1306915``.

    Without the glued form the flag rule matched no kind and the probe was
    refused as one that cannot fail, although the scheduler query it runs can
    plainly differ between sweeps.
    """
    assert recovery_wait._wait_probe_reference_kind(GLUED_PROBE) == "job"
    assert recovery_wait._wait_probe_job_id(GLUED_PROBE) == "1306915"
    assert recovery_wait._wait_probe_cannot_fail(GLUED_PROBE) is False


def test_a_shell_probe_reads_exactly_as_it_did() -> None:
    """The shell path is unchanged by the interpreter reading.

    Its words are still split on the shell's separators, so the job id written
    next to a control operator still resolves as one.
    """
    assert recovery_wait._wait_probe_reference_kind(SHELL_PROBE) == "job"
    assert recovery_wait._wait_probe_job_id(SHELL_PROBE) == "1274028"
    assert recovery_wait._wait_probe_cannot_fail(SHELL_PROBE) is False
