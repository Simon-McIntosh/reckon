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

from reckon import _backends, nested_launch
from reckon.crew import dispatch_launch, fleet_node

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


def _submit(sandbox, tool, arguments, *, session=SESSION, run=RUN, environment=None):
    """Run one submission through the shim and read the scheduler's argv."""
    sandbox["record"].unlink(missing_ok=True)
    shim = nested_launch.shim_directory() / tool
    env = _environment(sandbox, session=session, run=run)
    env.update(environment or {})
    result = subprocess.run(
        [str(shim), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
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
def test_a_session_with_no_run_names_the_session(sandbox, tool):
    received = _submit(sandbox, tool, SUBMISSIONS[tool], run=None)

    comment = _comment(received)
    assert comment is not None, (
        f"{tool} submitted for a coordinator session with no run was given no "
        f"comment, so the job names no owner: {received}"
    )
    assert SESSION in comment, comment


@pytest.mark.parametrize("tool", TOOLS)
def test_a_submission_with_neither_session_nor_run_adds_nothing(sandbox, tool):
    received = _submit(sandbox, tool, SUBMISSIONS[tool], session=None, run=None)

    assert _comment(received) is None, (
        f"a submission with no session and no run in its environment was given "
        f"a comment naming an owner it does not have: {received}"
    )


def test_a_script_comment_survives_beside_the_ids(sandbox):
    script = sandbox["tmp"] / "nightly.sh"
    script.write_text(
        "#!/bin/sh\n#SBATCH --partition=all_debug\n"
        "#SBATCH --comment=nightly calibration\necho task\n",
        encoding="utf-8",
    )

    received = _submit(sandbox, "sbatch", [str(script)])

    comment = _comment(received)
    assert comment is not None, received
    assert "nightly calibration" in comment, (
        f"the comment declared inside the submitted script was dropped: {comment}"
    )
    assert SESSION in comment and RUN in comment, comment


def test_an_option_after_a_script_comment_is_still_a_directive():
    comment, kept = nested_launch._script_comment(
        ["#SBATCH --comment=a --partition=gpu"]
    )

    assert comment == "a", comment
    assert kept == ["#SBATCH --partition=gpu"], (
        f"an option declared beside the script's comment was swallowed into "
        f"the comment text: {kept}"
    )


def test_a_multi_word_script_comment_keeps_its_words():
    comment, kept = nested_launch._script_comment(
        ["#SBATCH --comment=nightly calibration", "#SBATCH --partition=all_debug"]
    )

    assert comment == "nightly calibration", comment
    assert kept == ["#SBATCH --partition=all_debug"], kept


def test_a_fleet_allocation_is_found_once_its_comment_gains_an_owner():
    owner = "reckon session=0f6b2a4c-session run=r-20260101T000000000000-fleetish"
    rows = [
        {"jobid": "1", "comment": f"{fleet_node.FLEET_COMMENT} {owner}"},
        {"jobid": "2", "comment": f"{fleet_node.fleet_comment('nightly')} {owner}"},
        {"jobid": "3", "comment": "an unrelated job"},
    ]

    catch_all = fleet_node.find_allocations(rows)
    assert [job["jobid"] for job in catch_all] == ["1"], catch_all
    assert fleet_node.fleet_of_allocation(rows[0]) == fleet_node.CATCH_ALL_FLEET
    assert fleet_node.find_allocations(rows, fleet="nightly")[0]["jobid"] == "2"
    assert fleet_node.fleet_of_allocation(rows[1]) == "nightly"
    assert fleet_node.find_allocations(rows, fleet="other") == []


def test_a_composed_dispatch_launch_hands_its_worker_the_session(
    sandbox, tmp_path, monkeypatch
):
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch-root"))
    plan = _backends.launch_plan(
        backend_name="claude",
        backend={"launch": "cli", "command": "claude", "sandbox": "worktree-full"},
        prompt="do the work",
        worktree=str(tmp_path),
        fence=False,
    )
    # The session is what the run's record carries, read through the launch's
    # own reader rather than spelled out here, so a change to where the record
    # keeps it moves this case with the code.
    record = {"session": SESSION}
    composed = dispatch_launch._worker_runtime_plan(
        plan,
        run_id=RUN,
        manifest_path=str(tmp_path / "manifest.md"),
        attempt_started_at="2026-01-01T00:00:00Z",
        coordinator_session=dispatch_launch.coordinator_session(record),
    )

    assert composed.environment.get("RECKON_SESSION_ID") == SESSION, (
        f"a dispatched worker's launch environment does not carry the session "
        f"its run record names: {composed.environment.get('RECKON_SESSION_ID')!r}"
    )
    received = _submit(
        sandbox,
        "sbatch",
        SUBMISSIONS["sbatch"],
        environment={
            "RECKON_RUN_ID": composed.environment["RECKON_RUN_ID"],
            "RECKON_SESSION_ID": composed.environment["RECKON_SESSION_ID"],
        },
    )
    comment = _comment(received)
    assert comment is not None and SESSION in comment and RUN in comment, comment


def test_the_owner_comment_names_the_session_before_the_run():
    owner = nested_launch.owner_comment(
        {"RECKON_SESSION_ID": SESSION, "RECKON_RUN_ID": RUN}
    )

    assert owner == f"reckon session={SESSION} run={RUN}"


def test_a_run_without_a_session_still_names_the_run():
    owner = nested_launch.owner_comment({"RECKON_RUN_ID": RUN})

    assert owner == f"reckon run={RUN}"


def test_a_session_with_no_run_is_named_by_the_comment():
    assert (
        nested_launch.owner_comment({"RECKON_SESSION_ID": SESSION})
        == f"reckon session={SESSION}"
    )


def test_no_session_and_no_run_means_no_comment():
    assert nested_launch.owner_comment({}) == ""
