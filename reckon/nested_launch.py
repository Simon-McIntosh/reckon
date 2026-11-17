"""Refuse an implicit nested launch from inside the held SLURM job.

A shell that is already inside the allocation does not need a scheduler. An
implicit ``srun`` from there inherits the job's task count and fans out across
the whole allocation, so a single command turns into a per-task swarm. This
module is the decision the ``srun``/``sbatch``/``salloc`` shims under
:mod:`reckon.host_shims` compose their behaviour from.

The answer comes from :func:`reckon.host.host_facts`, whose ``in_allocation``
is taken from the process's own cgroup line rather than from ``SLURM_JOB_ID``
(a variable a shell outside the job inherits). Inside the allocation a launch
is refused with status 97 unless one of these things holds:

* it is an ``sbatch`` naming a partition explicitly (``--partition`` or
  ``-p``), which submits a job of its own; or
* the caller set ``RECKON_ALLOW_NESTED_LAUNCH`` — the deliberate discharge,
  named in every refusal so the escape is one visible step rather than a
  default; or
* it is an ``srun`` carrying ``--overlap`` together with an explicit
  ``--jobid`` or a single task (``--ntasks=1`` / ``-n 1``), because such a
  launch names the one step it wants instead of inheriting the task count.

Every refusal also names both routes out, so a worker that reads it does not
take it for "no route exists": heavy work that does not belong in this
allocation goes to a separate partition job, submitted with ``sbatch``, and a
launch that is meant to be nested is the deliberate discharge above. It says
plainly that an ``srun`` from inside the allocation runs as a step of the held
job whatever partition it names, so the opt-in is never mistaken for the way
to reach a separate partition.

Outside the allocation, and for every argv that is not refused, the shim
forwards to the real binary. A fenced worker carries its mount recipe in the
process environment; sbatch and srun wrap the task with that recipe before
forwarding, because the scheduler starts outside the submitting namespace. The
real binary is the first one on ``PATH`` that is not a reckon shim (:mod:`reckon.shim_lookup`), so
the shim never execs itself or the shim of another checkout on the same
``PATH``. The nested-launch decision never changes the resource request.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from reckon.host import HostFacts, host_facts
from reckon.shim_lookup import (
    caller_environment,
    deeper,
    depth_refusal,
    real_executable,
)

# The status a refusal exits with. Distinct from the codes used by the real
# scheduler tools so a caller can tell a refusal from a launch failure.
REFUSAL_STATUS = 97

# The status returned when no real binary can be found outside the shim.
MISSING_BINARY_STATUS = 127

# The one discharge from the refusal. Set to a truthy value and the launch is
# forwarded unchanged.
OVERRIDE_ENV = "RECKON_ALLOW_NESTED_LAUNCH"

# The only channel a fence travels to a submitted job on: the worker's own
# composed mounts ride this variable, set by the fence's ``--setenv`` on the
# worker argv and again on the task. Nothing recomposes a fence inside a job,
# so a job's roots are its worker's roots by construction, and a worker with no
# fence hands no fence down. One name, read by the shim, the task bootstrap and
# :func:`job_fence`, so the three cannot disagree about where the fence lives.
WORKER_FENCE_ENV = "RECKON_WORKER_FENCE"

# The other route, and the partitions that show what a separate partition job
# looks like here. The verb is part of the route: a separate partition job is
# submitted with ``sbatch``, and with ``sbatch --wait`` when the caller needs
# the result before it continues. The opt-in above is for a launch that is
# meant to be nested; work that simply does not belong inside this allocation
# goes to a job of its own, which is this route. Both are named in every
# refusal, so a reader learns where the work should go rather than only that it
# was refused.
PARTITION_ROUTE = (
    "a separate partition job, submitted with sbatch "
    "(sbatch --wait when the caller needs the result before it continues)"
)
PARTITION_ROUTE_VERB = "sbatch"
PARTITION_EXAMPLES = ("all_debug", "betelgeuse")

# What an ``srun`` does from inside the allocation, whatever partition it names
# and whether or not the opt-in is set: it opens a step inside this job rather
# than a job of its own, so it cannot move the work off the node it already
# runs on. Naming this beside the route above is what keeps a reader from
# taking the opt-in as the way to reach a separate partition.
NESTED_SRUN_SENTENCE = (
    "An srun from inside the allocation runs as a step of the held job, with "
    "or without the opt-in and whatever partition it names; only sbatch "
    "submits a job of its own."
)

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


# Argument roles from Slurm src/common/slurm_opt.c: common_options filtered
# by slurm_option_table_create for sbatch. Optional values attach to their
# option; required values consume the next token even when it starts with a dash.

_SBATCH_LONG_REQUIRED = frozenset(
    {
        "account",
        "acctg-freq",
        "array",
        "autocomplete",
        "batch",
        "bb",
        "bbf",
        "begin",
        "chdir",
        "cluster",
        "cluster-constraint",
        "clusters",
        "comment",
        "constraint",
        "container",
        "container-id",
        "context",
        "core-spec",
        "cores-per-socket",
        "cpu-freq",
        "cpus-per-gpu",
        "cpus-per-task",
        "deadline",
        "delay-boot",
        "dependency",
        "distribution",
        "error",
        "exclude",
        "export",
        "extra",
        "extra-node-info",
        "gid",
        "gpu-bind",
        "gpu-freq",
        "gpus",
        "gpus-per-node",
        "gpus-per-socket",
        "gpus-per-task",
        "gres",
        "gres-flags",
        "hint",
        "input",
        "job-name",
        "kill-on-invalid-dep",
        "licenses",
        "mail-type",
        "mail-user",
        "mcs-label",
        "mem",
        "mem-bind",
        "mem-per-cpu",
        "mem-per-gpu",
        "mincpus",
        "network",
        "nodefile",
        "nodelist",
        "nodes",
        "ntasks",
        "ntasks-per-core",
        "ntasks-per-gpu",
        "ntasks-per-node",
        "ntasks-per-socket",
        "ntasks-per-tres",
        "open-mode",
        "output",
        "partition",
        "power",
        "prefer",
        "priority",
        "profile",
        "qos",
        "reservation",
        "signal",
        "sockets-per-node",
        "switches",
        "tasks-per-node",
        "thread-spec",
        "threads-per-core",
        "time",
        "time-min",
        "tmp",
        "tres-bind",
        "tres-per-task",
        "uid",
        "wait-all-nodes",
        "wckey",
        "wrap",
    }
)

_SBATCH_SHORT_REQUIRED = frozenset("ABCDFGJLMNSabcdeimnopqtwx")

_SBATCH_LONG_OPTIONAL = frozenset(
    {
        "exclusive",
        "get-user-env",
        "nice",
        "no-kill",
        "propagate",
    }
)

_SBATCH_SHORT_OPTIONAL = frozenset("k")

_SBATCH_LONG_FLAGS = frozenset(
    {
        "contiguous",
        "help",
        "hold",
        "ignore-pbs",
        "no-requeue",
        "overcommit",
        "oversubscribe",
        "parsable",
        "quiet",
        "reboot",
        "requeue",
        "spread-job",
        "test-only",
        "usage",
        "use-min-nodes",
        "verbose",
        "version",
        "wait",
    }
)

_SBATCH_SHORT_FLAGS = frozenset("HOQVWhsv")


def _explicit_partition_submission(tool: str, argv: Sequence[str]) -> bool:
    """Read scheduler option roles without interpreting values or script args.

    Unknown options are refused because their value boundaries are unknown.
    The real scheduler still validates option values on a forwarded launch.
    """
    if tool != "sbatch":
        return False
    try:
        options, _operands = _scheduler_options(tool, argv)
    except ValueError:
        return False
    partitions = [
        value for name, value, _tokens in options if name in {"p", "partition"}
    ]
    partition = partitions[-1] if partitions else ""
    return bool(partition and partition.strip()) and not partition.startswith("-")


def refuses_implicit_launch(
    tool: str, argv: Sequence[str], facts: HostFacts, environ: Mapping[str, str]
) -> bool:
    """Whether ``tool`` with ``argv`` must be refused where it stands.

    Outside the allocation nothing is refused. Inside it, only the implicit
    launch is: sbatch may name a partition for a separate job, the override
    discharges deliberate nesting, and an explicit single-step ``srun`` names
    the step it wants.
    """
    return (
        facts.in_allocation
        and not _override_set(environ)
        and not _explicit_single_step(tool, argv)
        and not _explicit_partition_submission(tool, argv)
    )


def refusal_message(tool: str, facts: HostFacts) -> str:
    """The refusal an implicit nested launch composes.

    It names the job and the node, says the work should run directly, names
    both routes out — a separate partition job for heavy work, and the opt-in
    for a deliberate nested launch — states the explicit single-step form for
    ``srun``, and says outright that the command line is not rewritten. It
    never invents a corrected argv.
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
        (
            f"Heavy work that does not belong inside this job goes to "
            f"{PARTITION_ROUTE} instead (for example on "
            f"{' or '.join(PARTITION_EXAMPLES)})."
        ),
        "Submit it with sbatch --partition=<partition> (or sbatch -p <partition>).",
        NESTED_SRUN_SENTENCE,
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
    """The real ``tool`` on ``path``: never this shim, and never another one.

    The shim's own directory is dropped by resolved path, and any other
    candidate that is a reckon shim is skipped wherever it sits. Dropping only
    the own directory lets two checkouts' shims on one ``PATH`` exec each other
    in a loop that never reaches the scheduler.
    """
    return real_executable(tool, path, skipped=[shim_dir])


def shim_directory() -> Path:
    """The directory the shims live in, derived from this module's location."""
    return Path(__file__).resolve().parent / "host_shims"


# The bubblewrap options a composed fence is built from, grouped by what they
# leave at their destination, so a seal and a grant are read from the argv that
# emits them rather than from a second composition that could disagree with it.
# This table is the fence's own: the launch-time reader
# ``reckon.crew.dispatch_launch._FENCE_BIND_FLAGS`` answers a different question
# (which *sources* to retry from a vanished bind) and names only the three
# options it composes, so it neither knows the ``-try`` forms nor the mask this
# reader reports.
_SEAL_OPTIONS = ("--ro-bind", "--ro-bind-try")
_GRANT_OPTIONS = ("--bind", "--bind-try", "--dev-bind", "--dev-bind-try")
_MASK_OPTIONS = ("--tmpfs",)
_TWO_PATH_OPTIONS = frozenset((*_SEAL_OPTIONS, *_GRANT_OPTIONS))


@dataclass(frozen=True)
class JobFence:
    """The roots one fence argv seals, grants and masks, in composition order.

    This is a reading of a composed fence, not a second model of one. The argv
    is the whole answer: the mounts it emits are the mounts a job under it
    holds, and nothing here recomposes them from the parameters a fence was
    built from. Paths are reported in the order the argv mounts them, duplicate
    destinations included, so a seal emitted before a grant that covers it still
    reads in that order.
    """

    sealed: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()
    masked: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        """Return the report as JSON-ready lists."""
        return {
            "sealed": list(self.sealed),
            "writable": list(self.writable),
            "masked": list(self.masked),
        }


def fence_roots(argv: Sequence[str]) -> JobFence:
    """Read a composed fence argv into the roots it seals, grants and masks.

    A destination is reported for every mount option the fence emits, and only
    the options named above are understood: an argument that does not start a
    recognised option is a value and is skipped by the option that consumed it.
    That is the whole grammar the fence emitter uses, so a fence this module did
    not compose still reads as the mounts it names rather than as a guess.
    """
    sealed: list[str] = []
    writable: list[str] = []
    masked: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            break
        if argument in _TWO_PATH_OPTIONS:
            # source then destination; a truncated argv mounts nothing.
            if index + 2 >= len(argv):
                break
            destination = argv[index + 2]
            (sealed if argument in _SEAL_OPTIONS else writable).append(destination)
            index += 3
            continue
        if argument in _MASK_OPTIONS:
            if index + 1 >= len(argv):
                break
            masked.append(argv[index + 1])
            index += 2
            continue
        index += 1
    return JobFence(tuple(sealed), tuple(writable), tuple(masked))


def job_fence(environ: Mapping[str, str]) -> JobFence | None:
    """The fence a process in a submitted job runs under, or None if unfenced.

    The recipe its worker's own fence put in the environment is the only source:
    a job's sealed and writable roots are its worker's because they are the same
    mounts, never because a fence was composed again here. No recipe means the
    worker itself ran unfenced — an in-harness worker, or a dispatch launched
    with ``--no-fence REASON`` — and its jobs run unfenced too, with the reason
    on the run's row. A recipe that cannot be read is an error rather than an
    absent fence: reporting "unfenced" for a fence that is present would claim
    the opposite protection from the one the job holds.
    """
    encoded = str(environ.get(WORKER_FENCE_ENV) or "")
    if not encoded:
        return None
    try:
        recipe = json.loads(base64.urlsafe_b64decode(encoded))
        argv = recipe["argv"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"{WORKER_FENCE_ENV} carries no readable fence recipe: {exc}"
        ) from exc
    if not isinstance(argv, list):
        raise TypeError(f"{WORKER_FENCE_ENV}'s recipe names no fence argv")
    return fence_roots([str(word) for word in argv])


# The task bootstrap uses only the standard library, and carries its recipe in
# argv so sbatch --export=NONE cannot discard the fence. The child gets the
# caller's stdin unchanged, including srun's redirected input.
_TASK_REENTRY = """import base64, json, os, pathlib, subprocess, sys, tempfile
spec = json.loads(base64.urlsafe_b64decode(sys.argv[-1]))
recipe = spec["fence"]
if not spec.get("sources_prepared"):
    # Only sources known to be directory grants may be supplied empty. A
    # missing credential or executable must still refuse the task.
    directory_sources = set(recipe.get("directory_sources", ()))
    prefix = recipe["argv"]
    seals = ("--ro-bind", "--ro-bind-try")
    grants = ("--bind", "--bind-try", "--dev-bind", "--dev-bind-try")
    index = 1
    while index < len(prefix):
        option = prefix[index]
        if option in (*seals, *grants):
            source = prefix[index + 1]
            if option in grants and source in recipe.get("directories", ()):
                directory_sources.add(source)
            if (option in seals and source == prefix[index + 2]
                    and not os.path.lexists(source)):
                prefix[index] = "--ro-bind-try"
            index += 3
        else:
            index += 1
    missing = sorted(pathlib.Path(p) for p in directory_sources
                     if not os.path.lexists(p))
    if missing:
        # Build source directories in a private mount namespace. Existing
        # children remain bind-mounted; only absent paths get empty tmpfs.
        # The scaffolding is read-only, then the inherited fence re-opens
        # exactly its grants. No mkdir touches the job node's filesystem.
        ancestors = set()
        for path in missing:
            parent = path.parent
            while not parent.exists():
                parent = parent.parent
            ancestors.add(parent)
        prepare = [prefix[0], "--dev-bind", "/", "/"]
        for parent in sorted(ancestors, key=lambda p: len(p.parts)):
            prepare += ["--tmpfs", str(parent)]
            for child in sorted(parent.iterdir()):
                if child.is_symlink():
                    prepare += ["--symlink", os.readlink(child), str(child)]
                else:
                    prepare += ["--dev-bind", str(child), str(child)]
        for path in missing:
            prepare += ["--tmpfs", str(path)]
        for parent in sorted(ancestors, key=lambda p: -len(p.parts)):
            prepare += ["--remount-ro", str(parent)]
        spec["sources_prepared"] = True
        encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
        os.execvp(prepare[0], [*prepare, "--", sys.executable, "-I", "-c",
                              sys.argv[1], sys.argv[1], encoded])
command = spec["command"]
script = None
try:
    if "script" in spec:
        fd, script = tempfile.mkstemp(prefix="reckon-task-", dir=recipe["scratch"])
        with os.fdopen(fd, "wb") as stream:
            stream.write(base64.b64decode(spec["script"]))
        os.chmod(script, 0o700)
        command = [script, *command]
    prefix = recipe["argv"]
    encoded = base64.urlsafe_b64encode(json.dumps(recipe).encode()).decode()
    prefix += ["--setenv", spec["env"], encoded]
    result = subprocess.run([*prefix, "--", *command], check=False)
finally:
    if script is not None:
        os.unlink(script)
raise SystemExit(result.returncode if result.returncode >= 0 else 128-result.returncode)
"""


def _task_command(
    recipe: Mapping, command: Sequence[str], *, script: bytes | None = None
) -> list[str]:
    """Carry a fence to the scheduler task without depending on exported env."""
    spec = {"fence": recipe, "command": list(command), "env": WORKER_FENCE_ENV}
    if script is not None:
        spec["script"] = base64.b64encode(script).decode()
    encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
    return [sys.executable, "-I", "-c", _TASK_REENTRY, _TASK_REENTRY, encoded]


# Task-boundary parsing is deliberately strict: an unknown option may consume
# a command-looking value, so forwarding it could wrap the wrong executable.
_SRUN_REQUIRED = _SBATCH_LONG_REQUIRED | frozenset(
    {
        "bcast",
        "bcast-exclude",
        "broadcast",
        "compress",
        "cpu-bind",
        "epilog",
        "export-file",
        "het-group",
        "jobid",
        "kill-on-bad-exit",
        "label",
        "max-threads",
        "mpi",
        "msg-timeout",
        "prolog",
        "pty",
        "relative",
        "resv-ports",
        "slurmd-debug",
        "task-epilog",
        "task-prolog",
        "unbuffered",
        "wait",
        "threads",
    }
) - {"wrap", "label", "pty", "unbuffered", "compress", "resv-ports", "kill-on-bad-exit"}
_SRUN_FLAGS = _SBATCH_LONG_FLAGS | frozenset(
    {
        "disable-status",
        "enable-status",
        "exact",
        "label",
        "launch-cmd",
        "no-allocate",
        "overlap",
        "preserve-env",
        "quit-on-interrupt",
        "unbuffered",
    }
) - {"wait"}
_SRUN_OPTIONAL = _SBATCH_LONG_OPTIONAL | frozenset(
    {"compress", "immediate", "kill-on-bad-exit", "pty", "resv-ports", "send-libs"}
)
_SRUN_SHORT_REQUIRED = frozenset("ABCDFGJLMNRSTWbcdeimnopqrtwx")
_SRUN_SHORT_FLAGS = frozenset("EHOQVZXhlsuv")


def _scheduler_options(
    tool: str, argv: Sequence[str]
) -> tuple[list[tuple[str, str | None, list[str]]], list[str]]:
    """Read option roles once for nesting admission and task-boundary wrapping."""
    required = _SBATCH_LONG_REQUIRED if tool == "sbatch" else _SRUN_REQUIRED
    flags = _SBATCH_LONG_FLAGS if tool == "sbatch" else _SRUN_FLAGS
    optional = _SBATCH_LONG_OPTIONAL if tool == "sbatch" else _SRUN_OPTIONAL
    short_required = (
        _SBATCH_SHORT_REQUIRED if tool == "sbatch" else _SRUN_SHORT_REQUIRED
    )
    short_flags = _SBATCH_SHORT_FLAGS if tool == "sbatch" else _SRUN_SHORT_FLAGS
    options = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            return options, list(argv[index + 1 :])
        if argument == "-" or not argument.startswith("-"):
            return options, list(argv[index:])
        start = index
        value = None
        if argument.startswith("--"):
            name, attached, value = argument[2:].partition("=")
            if name in required:
                if not attached:
                    index += 1
                    if index >= len(argv):
                        raise ValueError(f"{tool} --{name} needs a value")
                    value = argv[index]
            elif name in flags:
                if attached:
                    raise ValueError(f"{tool} --{name} does not take a value")
            elif name not in optional:
                raise ValueError(f"unknown {tool} option --{name}")
        else:
            name = ""
            for offset, name in enumerate(argument[1:], 2):
                if name in short_required:
                    value = argument[offset:]
                    if not value:
                        index += 1
                        if index >= len(argv):
                            raise ValueError(f"{tool} -{name} needs a value")
                        value = argv[index]
                    break
                if name in _SBATCH_SHORT_OPTIONAL or (
                    tool == "srun" and name in {"I", "K"}
                ):
                    value = argument[offset:]
                    break
                if name not in short_flags:
                    raise ValueError(f"unknown {tool} option -{name}")
        options.append((name, value, list(argv[start : index + 1])))
        index += 1
    return options, []


def _batch_directives(script: bytes) -> list[str]:
    """Keep Slurm directives only where sbatch itself reads them."""
    directives = []
    for line in script.decode().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break
        if stripped.startswith("#SBATCH"):
            directives.append(line)
    return directives


def fenced_scheduler_task(
    tool: str, argv: Sequence[str], environ: Mapping[str, str]
) -> list[str]:
    """Re-enter the submitting fence at the task boundary, before user code.

    A direct absolute scheduler call bypasses this PATH shim. Interpreter file
    permissions remain the independent guard on that route.
    """
    encoded = environ.get(WORKER_FENCE_ENV)
    if not encoded or tool not in {"sbatch", "srun"}:
        return list(argv)
    recipe = json.loads(base64.urlsafe_b64decode(encoded))
    parsed, operands = _scheduler_options(tool, argv)
    if ":" in operands:
        raise ValueError(
            "heterogeneous scheduler launches cannot carry one fenced task"
        )
    if any(
        name in {"help", "usage", "version", "h", "V"}
        for name, _value, _tokens in parsed
    ):
        return list(argv)
    unsupported = {
        "multi-prog",
        "het-group",
        "launch-cmd",
        "task-prolog",
        "task-epilog",
        "prolog",
        "epilog",
    }
    if any(name in unsupported for name, _value, _tokens in parsed):
        raise ValueError(
            f"{tool} task hooks and multiple-program launches cannot carry a fenced task"
        )
    options = [
        token for name, _value, tokens in parsed if name != "wrap" for token in tokens
    ]
    wraps = [value for name, value, _tokens in parsed if name == "wrap"]
    wrap = wraps[-1] if wraps else None
    print(
        f"{tool}: task re-enters the worker fence; absolute scheduler paths bypass this guard.",
        file=sys.stderr,
    )
    if tool == "srun":
        if not operands:
            raise ValueError("fenced srun requires an executable")
        return [*options, *_task_command(recipe, operands)]
    if wrap is not None:
        if operands:
            raise ValueError("sbatch --wrap cannot take a script or script arguments")
        return [
            *options,
            "--wrap=" + shlex.join(_task_command(recipe, ["/bin/sh", "-c", wrap])),
        ]
    if operands and operands[0] != "-":
        script = Path(operands[0]).read_bytes()
    else:
        script = sys.stdin.buffer.read()
    if not script.startswith(b"#!"):
        raise ValueError("fenced sbatch script must start with an interpreter shebang")
    command = _task_command(recipe, operands[1:], script=script)
    wrapper = "\n".join(
        ["#!/bin/sh", *_batch_directives(script), "exec " + shlex.join(command), ""]
    )
    # The scheduler spools this stdin script before exec returns. An anonymous
    # file keeps it alive through exec without leaving a wrapper on disk.
    with tempfile.TemporaryFile() as stream:
        stream.write(wrapper.encode())
        stream.seek(0)
        os.dup2(stream.fileno(), sys.stdin.fileno())
    return options


def main(
    tool: str, argv: Sequence[str], *, environ: Mapping[str, str] | None = None
) -> int:
    """Run, or refuse, one shim invocation.

    Returns the refusal status when the launch is refused, and otherwise
    replaces this process with the real binary. The return is for the refusal
    and the missing-binary cases only; a forwarded launch does not come back.
    """
    env = caller_environment(os.environ if environ is None else environ)
    refusal = depth_refusal(tool, env)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return REFUSAL_STATUS
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
    # the exit status unchanged. The nesting count goes one higher with it.
    try:
        task_argv = fenced_scheduler_task(tool, argv, env)
    except (ValueError, OSError) as exc:
        print(f"{tool}: refusing an unfenced scheduler task: {exc}", file=sys.stderr)
        return REFUSAL_STATUS
    os.execve(found, [tool, *task_argv], deeper(env))  # noqa: S606
    raise AssertionError("os.execve returned; a forwarded launch cannot continue.")
