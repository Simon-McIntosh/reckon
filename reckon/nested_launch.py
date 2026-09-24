"""Refuse an implicit nested launch from inside the held SLURM job.

A shell that is already inside the allocation does not need a scheduler. An
implicit ``srun`` from there inherits the job's task count and fans out across
the whole allocation, so a single command turns into a per-task swarm. This
module is the decision the ``srun``/``sbatch``/``salloc`` shims under
:mod:`reckon.host_shims` compose their behaviour from.

The answer comes from :func:`reckon.host.host_facts`, whose ``in_allocation``
is taken from the process's own cgroup line rather than from ``SLURM_JOB_ID``
(a variable a shell outside the job inherits). Inside the allocation a launch
is refused with status 97 unless one of two things holds:

* the caller set ``RECKON_ALLOW_NESTED_LAUNCH`` — the deliberate discharge,
  named in every refusal so the escape is one visible step rather than a
  default; or
* it is an ``srun`` carrying ``--overlap`` together with an explicit
  ``--jobid`` or a single task (``--ntasks=1`` / ``-n 1``), because such a
  launch names the one step it wants instead of inheriting the task count.

Outside the allocation, and for every argv that is not refused, the shim
forwards to the real binary with the argv unchanged. The real binary is looked
up on ``PATH`` with the shim's own directory removed, so the shim never execs
itself. Nothing here runs a scheduler command, guesses a corrected command
line, or launches anything itself.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from reckon.host import HostFacts, host_facts

# The status a refusal exits with. Distinct from the codes used by the real
# scheduler tools so a caller can tell a refusal from a launch failure.
REFUSAL_STATUS = 97

# The status returned when no real binary can be found outside the shim.
MISSING_BINARY_STATUS = 127

# The one discharge from the refusal. Set to a truthy value and the launch is
# forwarded unchanged.
OVERRIDE_ENV = "RECKON_ALLOW_NESTED_LAUNCH"

# Truthy values that are not the discharge. Anything else non-empty enables it.
_OFF = frozenset({"", "0", "false", "no"})

# The tool whose argv may name an explicit single step. The others have no
# overlap form and are refused on the implicit launch alone.
_SRUN = "srun"


def _override_set(environ: Mapping[str, str]) -> bool:
    """Whether the caller asked, deliberately, for the nested launch."""
    value = environ.get(OVERRIDE_ENV, "")
    return str(value).strip().lower() not in _OFF


def _names_one_task(argv: Sequence[str]) -> bool:
    """Whether the argv names a single task explicitly."""
    for index, argument in enumerate(argv):
        if argument in ("--ntasks=1", "-n1"):
            return True
        if (
            argument in ("--ntasks", "-n")
            and index + 1 < len(argv)
            and argv[index + 1] == "1"
        ):
            return True
    return False


def _names_a_job(argv: Sequence[str]) -> bool:
    """Whether the argv names a job explicitly."""
    for index, argument in enumerate(argv):
        if argument.startswith("--jobid=") and argument[len("--jobid=") :]:
            return True
        if argument in ("--jobid", "-j") and index + 1 < len(argv) and argv[index + 1]:
            return True
    return False


def _explicit_single_step(tool: str, argv: Sequence[str]) -> bool:
    """Whether an srun argv asks for one explicit step rather than a fan-out.

    ``--overlap`` is what keeps the step inside the current allocation's
    resources; it only names a step the caller intended when it is paired with
    an explicit job or task count. An ``--overlap`` alone still inherits the
    job's task count, so it is not an exemption.
    """
    if tool != _SRUN or "--overlap" not in argv:
        return False
    return _names_one_task(argv) or _names_a_job(argv)


def refuses_implicit_launch(
    tool: str, argv: Sequence[str], facts: HostFacts, environ: Mapping[str, str]
) -> bool:
    """Whether ``tool`` with ``argv`` must be refused where it stands.

    Outside the allocation nothing is refused. Inside it, only the implicit
    launch is: the override discharges all of them, and an explicit single-step
    ``srun`` is already the shape the rule wants.
    """
    return (
        facts.in_allocation
        and not _override_set(environ)
        and not _explicit_single_step(tool, argv)
    )


def refusal_message(tool: str, facts: HostFacts) -> str:
    """The refusal an implicit nested launch composes.

    It names the job and the node, says the work should run directly, states
    the discharge and — for ``srun`` — the explicit form, and says outright
    that the command line is not rewritten. It never invents a corrected argv.
    """
    job = facts.job_id or "an unknown job"
    node = facts.node or "an unknown node"
    lines = [
        (
            f"refusing an implicit nested {tool}: this shell is already inside "
            f"SLURM job {job} on node {node}."
        ),
        (
            f"An implicit {tool} inherits the job's task count and fans out across "
            "the allocation; run the command directly instead. This shim does not "
            "rewrite the command line for you."
        ),
    ]
    if tool == _SRUN:
        lines.append(
            "To run one explicit step of this job, name the step: "
            f"srun --overlap --ntasks=1 <command> (or --overlap --jobid={job})."
        )
    lines.append(
        f"If the nested launch is deliberate, set {OVERRIDE_ENV}=1 and re-run."
    )
    return "\n".join(lines)


def real_binary(tool: str, shim_dir: Path, path: str) -> str | None:
    """The real ``tool`` on ``path`` with the shim's own directory removed.

    Removing the shim's directory is what stops the shim exec'ing itself: the
    lookup is by resolved directory, so a ``PATH`` entry that reaches the shim
    directory through a symlink is dropped too.
    """
    skipped = os.path.realpath(str(shim_dir))
    kept = [
        entry
        for entry in str(path).split(os.pathsep)
        if entry and os.path.realpath(entry) != skipped
    ]
    if not kept:
        return None
    return shutil.which(tool, path=os.pathsep.join(kept))


def shim_directory() -> Path:
    """The directory the shims live in, derived from this module's location."""
    return Path(__file__).resolve().parent / "host_shims"


def main(
    tool: str, argv: Sequence[str], *, environ: Mapping[str, str] | None = None
) -> int:
    """Run, or refuse, one shim invocation.

    Returns the refusal status when the launch is refused, and otherwise
    replaces this process with the real binary. The return is for the refusal
    and the missing-binary cases only; a forwarded launch does not come back.
    """
    env = os.environ if environ is None else environ
    facts = host_facts(environ=env)
    if refuses_implicit_launch(tool, argv, facts, env):
        print(refusal_message(tool, facts), file=sys.stderr)
        return REFUSAL_STATUS
    found = real_binary(tool, shim_directory(), env.get("PATH", os.defpath))
    if found is None:
        print(
            f"{tool} shim: no real {tool} on PATH outside {shim_directory()}",
            file=sys.stderr,
        )
        return MISSING_BINARY_STATUS
    # The argv is already split and the binary is resolved by absolute path;
    # there is no shell to route through, and exec is what forwards signals and
    # the exit status unchanged.
    os.execv(found, [tool, *argv])  # noqa: S606
    raise AssertionError("os.execv returned; a forwarded launch cannot continue.")
