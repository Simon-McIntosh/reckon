"""A scheduler job a worker submits names its coordinator session and run.

``squeue`` and ``scontrol show job`` give the job id and nothing about who
asked for it. Dispatch exports the run id and the coordinator session into
every worker's environment; the shims in front of ``sbatch``, ``srun`` and
``salloc`` are the one place every scheduler call passes through, so they add
both to the job's comment field. The scheduler here is a stand-in that records
the argv it was handed, so what is read is the bytes a real scheduler would
have received.

Declared negative control: with the session id unset in the submitting
environment, the comment assertion fails.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

import pytest

from reckon import nested_launch

# The stand-in scheduler: it records its own argv and launches nothing. NUL
# separated, because one fenced task argument is a program with newlines in it.
STAND_IN = """#!/bin/sh
printf '%s\\0' "$@" > "$RECKON_STANDIN_RECORD"
"""

TOOLS = ("sbatch", "srun", "salloc")

SESSION = "0f6b2a4c-session"
RUN = "r-20261121T010258718236-wlsr-a-scheduler-job-names-its-owner"

SUBMISSIONS = {
    "sbatch": ["--partition=all_debug", "--wrap=echo task"],
    "srun": ["--ntasks=1", "hostname"],
    "salloc": ["--partition=all_debug", "--ntasks=1"],
}


@pytest.fixture
def sandbox(tmp_path):
    """A stand-in scheduler after the shim on ``PATH``, and a record path."""
    scheduler = tmp_path / "scheduler-bin"
    scheduler.mkdir()
    for tool in TOOLS:
        stand_in = scheduler / tool
        stand_in.write_text(STAND_IN, encoding="utf-8")
        stand_in.chmod(0o755)
    (tmp_path / "home").mkdir()
    path = os.pathsep.join(
        [str(nested_launch.shim_directory()), str(scheduler), "/usr/bin:/bin"]
    )
    return {
        "tmp": tmp_path,
        "scheduler": scheduler,
        "record": tmp_path / "stand-in-argv",
        "path": path,
    }


def _fence_recipe(tmp_path):
    """The recipe a fenced worker's task re-enters the fence with."""
    return {
        "argv": ["/bin/sh", "-c", "true"],
        "program": "/bin/true",
        "carried_program": "",
        "directories": [],
        "scratch": str(tmp_path / "scratch"),
    }


def _environment(sandbox, *, session=SESSION, run=RUN):
    """What the submitting worker hands the shim, with nothing of this one."""
    excluded = {
        "RECKON_RUN_ID",
        "RECKON_SESSION_ID",
        "RECKON_CREW_SESSION",
        nested_launch.WORKER_FENCE_ENV,
        # This suite runs inside a worker fence, whose own relocation of the
        # scheduler tools would take the shim straight to the real binary and
        # past this test's stand-in.
        nested_launch.SHIM_REAL_DIRECTORY_ENV,
        nested_launch.SHIM_HOME_ENV,
    }
    environment = {
        name: value for name, value in os.environ.items() if name not in excluded
    }
    environment["RECKON_ALLOW_NESTED_LAUNCH"] = "1"
    environment["RECKON_SHIM_PYTHON"] = sys.executable
    environment["RECKON_STANDIN_RECORD"] = str(sandbox["record"])
    environment["PATH"] = sandbox["path"]
    environment["HOME"] = str(sandbox["tmp"] / "home")
    if run:
        environment["RECKON_RUN_ID"] = run
        environment[nested_launch.WORKER_FENCE_ENV] = base64.urlsafe_b64encode(
            json.dumps(_fence_recipe(sandbox["tmp"])).encode()
        ).decode()
    if session:
        environment["RECKON_SESSION_ID"] = session
    return environment


def _submit(sandbox, tool, arguments, *, session=SESSION, run=RUN):
    """Run one submission through the shim and read the scheduler's argv."""
    sandbox["record"].unlink(missing_ok=True)
    shim = nested_launch.shim_directory() / tool
    result = subprocess.run(
        [str(shim), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=_environment(sandbox, session=session, run=run),
    )
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert sandbox["record"].exists(), (
        f"{tool} was never forwarded to the scheduler: {result.stderr}"
    )
    return [
        word.decode() for word in sandbox["record"].read_bytes().split(b"\0") if word
    ]


def _comment(arguments):
    """The comment field the scheduler was handed, or ``None``."""
    for index, argument in enumerate(arguments):
        if argument.startswith("--comment="):
            return argument[len("--comment=") :]
        if argument == "--comment" and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


@pytest.mark.parametrize("tool", TOOLS)
def test_each_scheduler_tool_names_the_session_and_run(sandbox, tool):
    received = _submit(sandbox, tool, SUBMISSIONS[tool])

    comment = _comment(received)
    assert comment is not None, (
        f"{tool} reached the scheduler with no comment, so the job names no "
        f"owner: {received}"
    )
    assert SESSION in comment, comment
    assert RUN in comment, comment


@pytest.mark.parametrize("tool", TOOLS)
def test_a_callers_comment_is_kept_beside_the_ids(sandbox, tool):
    received = _submit(
        sandbox,
        tool,
        ["--comment=nightly calibration", *SUBMISSIONS[tool]],
    )

    comment = _comment(received)
    assert comment is not None, received
    assert "nightly calibration" in comment, (
        f"the caller's own comment was replaced rather than extended: {comment}"
    )
    assert SESSION in comment and RUN in comment, comment


@pytest.mark.parametrize("tool", TOOLS)
def test_a_submission_outside_any_run_adds_nothing(sandbox, tool):
    received = _submit(sandbox, tool, SUBMISSIONS[tool], run=None)

    assert _comment(received) is None, (
        f"a submission with no run in its environment was given a comment "
        f"naming an owner it does not have: {received}"
    )


def test_the_owner_comment_names_the_session_before_the_run():
    owner = nested_launch.owner_comment(
        {"RECKON_SESSION_ID": SESSION, "RECKON_RUN_ID": RUN}
    )

    assert owner == f"reckon session={SESSION} run={RUN}"


def test_a_run_without_a_session_still_names_the_run():
    owner = nested_launch.owner_comment({"RECKON_RUN_ID": RUN})

    assert owner == f"reckon run={RUN}"


def test_no_run_means_no_comment_even_with_a_session():
    assert nested_launch.owner_comment({"RECKON_SESSION_ID": SESSION}) == ""
