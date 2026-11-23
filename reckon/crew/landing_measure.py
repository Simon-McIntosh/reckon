"""Measure a delivered run's suite before the landing pass lands it.

The landing pass lands a delivered implementation run only when the run's own
suite shows no regression the base revision did not already show. Three
readings decide it:

* the run's armed command, run at the base revision and at the delivered head;
* the importer population — the test files importing each module the run
  changed — run at both revisions in parallel;
* every test id that fails at the head and not at the base, re-run alone three
  times at each revision, so a flake is told apart from a regression.

An id is a regression when it fails at the head more often than at the base.

The armed command is written the way a shell reads it: leading ``NAME=value``
tokens set the environment every arm runs under, and ``{node_test_files}``
stands for the node's own fenced test files. Both are resolved before any arm
starts. The base tree is given the ``.venv`` link a provisioned worktree
carries, so a command naming the project's interpreter by a relative path can
start there, and the node's test files the base revision lacks are copied in
from the head so the base arm measures the test the node added.

Every subprocess the measurement starts — the suite arms, the isolated re-runs
and the base checkout's git worktree calls — carries a wall-clock bound. A run
that exceeds it is unmeasurable rather than failing, so the landing is refused
and nothing is read from a partial log.

An arm's bound is not a fixed figure: it is sized from what the arms have taken
before. Every arm records its elapsed seconds and the number of files it read,
so the stored measurements give a project's seconds per file, and an arm whose
population is bigger than the bound anticipated — or whose host is slower than
it was — gets a bound that follows its own history. A project with no history
falls back to the configured floor.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from reckon.clones import changed_paths
from reckon.crew.dispatch_launch import worker_scratch_root
from reckon.crew.review import failure_ids_covered

#: How many times an added id is re-run alone at each revision.
REPEATS = 3

#: Parallelism for the importer population.
POPULATION_WORKERS = 8

#: Wall-clock bound for one measurement subprocess, in seconds. A flight config
#: may retune it under ``landing.measure_timeout_seconds``; this is the floor a
#: project with no measurement history is bounded by, and it is deliberately
#: modest so an unreadable arm releases the landing worker promptly.
DEFAULT_MEASURE_TIMEOUT_SECONDS = 600.0

# mktemp creates a private directory beneath this node-local root.
NODE_LOCAL_TEMP_ROOT = "/tmp"  # noqa: S108

#: How many times a population's own measured seconds per file an arm's bound
#: is. A bound of one multiple would time out an arm that simply repeated its
#: last reading, so the multiple leaves room for a busier host while still
#: bounding the arm.
BOUND_MULTIPLE = 2.0

#: A line a failing test runner reports its failing id on.
_FAILED_ID = re.compile(r"^(?:FAILED|ERROR)[ \t]+(\S+)", re.MULTILINE)

#: The receipt a partitioned arm's own EXIT trap prints. The scheduler writes
#: lines of its own after it — an ``srun: error: … Exited with exit code N``
#: line, for one — so the receipt is read on its own line and not as the log's
#: last one. The last match wins: a line the suite itself printed earlier is
#: superseded by the trap's own, which is written after everything the arm ran.
_EXIT_RECEIPT = re.compile(r"^EXIT=(\d+)[ \t]*$", re.MULTILINE)

#: The sentence a partitioned arm prints when a test file its tree lacked could
#: not be carried from the revision asked for it, with both facts in the line.
_TREE_BUILD_REFUSAL = re.compile(
    r"^cannot build the measurement tree: .*$", re.MULTILINE
)

#: The line the scheduler writes when it stops a job at its own time limit. A
#: job cancelled that way never reaches the EXIT trap its script set, so its
#: log carries no receipt and the scheduler's line is the only account of why
#: the arm has no result.
_TIME_LIMIT_CANCEL = re.compile(
    r"^(?:slurmstepd: error|srun: error):.*(?:DUE TO TIME LIMIT|Time limit exceeded)",
    re.IGNORECASE | re.MULTILINE,
)

#: The job id a scheduler line names, for a cancellation whose submission's
#: printed id cannot be read. A step cancelled by the time limit is named by
#: ``STEP <job>.<step>``, and the STEP id's integer part is the job.
_SCHEDULER_JOB = re.compile(r"\b(?:JOB|STEP) (\d+)\b")

#: The token an armed command carries in place of the node's own test files.
_SCOPED_PLACEHOLDER = "{node_test_files}"

#: A leading ``NAME=value`` token, which is the arm's environment rather than
#: the program it starts.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_RUN = subprocess.run
_SCHEDULER_RUN = subprocess.run

# The limit covers whole measurements, including their isolated re-runs.
MEASUREMENT_WIDTH_ENV = "RECKON_LANDING_MEASUREMENT_WIDTH"
DEFAULT_MEASUREMENT_WIDTH = 2
_ACTIVE_JOB_STATES = frozenset(
    {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED"}
)


class MeasurementJobError(ValueError):
    """A terminal scheduler job cannot supply evidence for its landing."""


def measurement_width() -> int:
    """The number of scheduler measurements the lander may have in flight."""
    try:
        return max(1, int(os.environ.get(MEASUREMENT_WIDTH_ENV, "2")))
    except ValueError:
        return DEFAULT_MEASUREMENT_WIDTH


_MEASUREMENT_AVAILABLE = threading.Condition()


@contextlib.contextmanager
def _promotion_measurement_slot():
    """Park remote measurements outside preparation capacity, at bounded width."""
    from reckon.crew.promotion_requests import (
        PREPARATION_DONE,
        PROMOTION_STAGE,
        claim_promotion_lease,
        release_promotion_lease,
    )

    handoff = PREPARATION_DONE.get()
    if handoff is None:
        yield
        return
    handoff()
    mark = PROMOTION_STAGE.get()
    if mark is not None:
        mark("measurement-wait")
    with _MEASUREMENT_AVAILABLE:
        while True:
            lease = next(
                (
                    claimed
                    for number in range(measurement_width())
                    if (
                        claimed := claim_promotion_lease(
                            f"promotion-measurement-slot-{number}"
                        )
                    )
                    is not None
                ),
                None,
            )
            if lease is not None:
                break
            _MEASUREMENT_AVAILABLE.wait(2.0)
    from reckon.crew.promotion import _PromotionLeaseRenewer

    renewer = _PromotionLeaseRenewer(lease)
    renewer.start()
    if mark is not None:
        mark("measurement")
    try:
        yield
    finally:
        renewer.stop()
        with _MEASUREMENT_AVAILABLE:
            release_promotion_lease(lease)
            _MEASUREMENT_AVAILABLE.notify_all()
        if mark is not None:
            mark("post-measurement")


def _handoff_measurement(function):
    @wraps(function)
    def measured(*args, **kwargs):
        with _promotion_measurement_slot():
            return function(*args, **kwargs)

    return measured


def submit_measurement(
    request_path: Path, *, config: Mapping[str, Any], bound: float
) -> dict[str, Any]:
    """Submit a whole measurement and return its durable scheduler identity."""
    import sys

    request = json.loads(request_path.read_text(encoding="utf-8"))
    from reckon.crew.promotion_gate import _unused_log_path

    log = _unused_log_path(request_path.parent / "job.log")
    source = Path(__file__).resolve().parents[2]
    invocation = shlex.join(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys; "
                "from reckon.crew.queue_landing import run_measurement_request; "
                "run_measurement_request(Path(sys.argv[1]))"
            ),
            str(request_path),
        ]
    )
    script = f"""set -euo pipefail
scratch=
trap 'status=$?; if [ -n "$scratch" ]; then rm -rvf "$scratch"; fi; printf "EXIT=%s\\n" "$status"' EXIT
export TMPDIR={NODE_LOCAL_TEMP_ROOT}
scratch=$(mktemp -d "$TMPDIR/$USER-landing-job-XXXXXX")
trap 'exit 143' TERM INT
export TMPDIR="$scratch" RECKON_WORKER_SCRATCH_ROOT="$scratch"
export PYTHONPATH={shlex.quote(str(source))}
{invocation}
"""
    landing = config.get("landing") or {}
    selected, argv, environment = partition_submission(
        script=script,
        partition=str(landing.get("measure_partition") or "all_debug"),
        long_partition=str(landing.get("measure_long_partition") or "rigel"),
        bound_seconds=bound,
        chdir=Path(request["pointer"]["repo"]),
        log_path=log,
        config=config,
    )
    argv.remove("--wait")
    finished = _SCHEDULER_RUN(
        argv, capture_output=True, text=True, check=False, timeout=30, env=environment
    )
    match = re.fullmatch(r"(\d+)(?:;[^\n]+)?\n?", finished.stdout or "")
    if finished.returncode or not match:
        raise ValueError(
            f"measurement submission failed; log {log}: "
            f"{finished.stderr or finished.stdout or finished.returncode}"
        )
    return {
        "job_id": match.group(1),
        "log_path": str(log),
        "request_path": str(request_path),
        "result_path": str(request_path.parent / "result.json"),
        "partition": selected,
        "bound_seconds": bound,
        "submitted_at": time.time(),
        "status": "pending",
        "head": request["head"],
        "primary": request["primary"],
        "command": request["pointer"]["suite_command"],
    }


def measurement_job_state(job: Mapping[str, Any]) -> str:
    """Read one scheduler observation; an absent accounting row is unknown.

    The queue forgets a job some minutes after it ends and then refuses a
    query naming it, so a refused queue read says only that the job is no
    longer queued. The accounting record is asked next, and it is the one
    whose failure leaves the state unknown.
    """
    job_id = str(job["job_id"])
    for argv in (
        ["squeue", "-h", "-j", job_id, "-o", "%T"],
        ["sacct", "-n", "-X", "-j", job_id, "--format=State", "--parsable2"],
    ):
        finished = _SCHEDULER_RUN(
            argv, capture_output=True, text=True, check=False, timeout=10
        )
        if finished.returncode and argv[0] == "squeue":
            continue
        if finished.returncode:
            raise ValueError(
                f"job {job_id}, log {job['log_path']}: scheduler query failed: "
                f"{finished.stderr or finished.stdout}"
            )
        states = (finished.stdout or "").strip().splitlines()
        if states:
            return states[0].strip().split("|")[0].split()[0].rstrip("+")
    return "UNKNOWN"


def active_measurement_jobs(pointers: Iterable[Mapping[str, Any]]) -> set[str]:
    """Count scheduler work even when its delivery currently waits on review."""
    active = set()
    for pointer in pointers:
        job = pointer.get("landing_measurement_job")
        if not isinstance(job, Mapping) or job.get("status") != "pending":
            continue
        try:
            state = measurement_job_state(job)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            state = "UNKNOWN"
        if state in _ACTIVE_JOB_STATES or state == "UNKNOWN":
            active.add(str(pointer["run_id"]))
    return active


def read_measurement_job(job: Mapping[str, Any]) -> Measurement | None:
    """A terminal job needs both a successful receipt and a matching result."""
    job_id, log_path = str(job["job_id"]), str(job["log_path"])
    state = measurement_job_state(job)
    if state in _ACTIVE_JOB_STATES or state == "UNKNOWN":
        return None
    if state != "COMPLETED":
        raise MeasurementJobError(
            f"measurement job {job_id} ended {state}; log {log_path}"
        )
    try:
        output = Path(log_path).read_text(encoding="utf-8")
        record = json.loads(Path(str(job["result_path"])).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise MeasurementJobError(
            f"measurement job {job_id} receipt unreadable; log {log_path}: {exc}"
        ) from exc
    error = ""
    if not re.search(r"^EXIT=0\n?\Z", output, re.MULTILINE):
        error = "the job has no terminal EXIT=0 receipt"
    elif not isinstance(record, Mapping):
        error = "the job result is not a measurement record"
    if error:
        raise MeasurementJobError(
            f"measurement job {job_id} receipt unreadable; log {log_path}: {error}"
        )
    measured = Measurement.from_dict(record)
    if measured.head != job["head"] or measured.command != job["command"]:
        error = "the job result names a different delivery"
    elif measured.population.get("main_sha") != job["primary"]:
        error = "the job result names a different primary"
    if error:
        raise MeasurementJobError(
            f"measurement job {job_id} receipt unreadable; log {log_path}: {error}"
        )
    return measured


@dataclass(frozen=True)
class Arm:
    """One command run at one revision, with what it reported."""

    command: tuple[str, ...]
    exit_status: int | None
    failure_ids: tuple[str, ...]
    log_path: str = ""
    timed_out: bool = False
    #: Why a submitted arm's log could not be read, when the reason is known:
    #: a log with no receipt, or one whose receipt disagrees with the status
    #: the scheduler returned. Empty when the arm simply has no status.
    unreadable_reason: str = ""
    #: Whether the arm's submission exited before the scheduler accepted a job,
    #: so the arm never ran at all. ``unreadable_reason`` then names the
    #: submission command, its status and its own output, which is what the
    #: landing's refusal quotes rather than reporting a silent absence.
    submission_failed: bool = False
    #: Wall-clock seconds the arm took, and how many files its population
    #: read. The pair is what a later arm's bound is sized from, so an arm
    #: that took 900 s is not judged again under a bound that ignores it.
    elapsed_seconds: float = 0.0
    file_count: int = 0
    #: The scheduler job a partitioned arm ran as and the time limit its
    #: submission carried. Both are empty for an arm that ran in place.
    job_id: str = ""
    time_limit: str = ""

    @property
    def readable(self) -> bool:
        """Whether the arm ran far enough to say which ids failed.

        Pytest exits 1 when tests fail and 2 when collection itself errored;
        both name what failed (``FAILED`` or ``ERROR`` lines). An exit of 2
        with named ids is the expected base reading of a node whose new test
        imports a symbol the node adds: the test cannot be collected before
        the change exists, so the base reports it failing. Exit 2 with no id
        named, or any other status, says nothing about which ids failed.
        """
        return self.exit_status == 0 or (
            self.exit_status in (1, 2) and bool(self.failure_ids)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_status": self.exit_status,
            "failure_ids": list(self.failure_ids),
            "log_path": self.log_path,
            "timed_out": self.timed_out,
            "unreadable_reason": self.unreadable_reason,
            "submission_failed": self.submission_failed,
            "elapsed_seconds": self.elapsed_seconds,
            "file_count": self.file_count,
            "job_id": self.job_id,
            "time_limit": self.time_limit,
        }


@dataclass(frozen=True)
class Measurement:
    """What the three readings found for one delivered run."""

    command: str
    suite: dict[str, Any]
    population: dict[str, Any]
    isolated: dict[str, dict[str, list[bool]]] = field(default_factory=dict)
    #: The run's scope as its delivered diff implies it: the test files the
    #: suite runs, the importers the population measures and the production
    #: neighbourhood a review reads. Empty when the run declared its own and
    #: kept them.
    scope: dict[str, Any] = field(default_factory=dict)
    real: tuple[str, ...] = ()
    unreadable: str = ""
    head: str = ""
    #: The project the measurement belongs to. A bound is sized from one
    #: project's own history, so a stored measurement says which project's
    #: seconds per file it contributes to.
    project: str = ""
    execution: str = "in-place"

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "head": self.head,
            "project": self.project,
            "execution": self.execution,
            "suite": self.suite,
            "population": self.population,
            "scope": self.scope,
            "isolated": self.isolated,
            "real": list(self.real),
            "flakes": [
                test_id
                for test_id in self.isolated
                if test_id not in self.real and not self.unreadable
            ],
            "unreadable": self.unreadable,
        }

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> Measurement:
        """Rebuild a stored measurement, so a pass can reuse one it recorded.

        A record that is missing a field is rebuilt with the empty value that
        field has for an unfinished measurement, never guessed: a stored record
        whose shape is unrecognisable yields an unreadable measurement rather
        than a clean one.
        """
        isolated = record.get("isolated")
        suite = record.get("suite")
        population = record.get("population")
        if not isinstance(isolated, Mapping) or not isinstance(suite, Mapping):
            return cls(
                command=str(record.get("command") or ""),
                suite={},
                population={},
                head=str(record.get("head") or ""),
                unreadable="the stored measurement is unreadable",
            )
        return cls(
            command=str(record.get("command") or ""),
            suite=dict(suite),
            population=dict(population) if isinstance(population, Mapping) else {},
            isolated={
                str(test_id): {
                    arm: [bool(value) for value in values]
                    for arm, values in arms.items()
                }
                for test_id, arms in isolated.items()
                if isinstance(arms, Mapping)
            },
            real=tuple(str(test_id) for test_id in record.get("real") or ()),
            scope=dict(record.get("scope") or {}),
            unreadable=str(record.get("unreadable") or ""),
            head=str(record.get("head") or ""),
            project=str(record.get("project") or ""),
            execution=str(record.get("execution") or "in-place"),
        )


def failing_ids(output: str) -> tuple[str, ...]:
    """The test ids one run reported as failing, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _FAILED_ID.finditer(output or ""):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def _is_real(base_failures: int, head_failures: int) -> bool:
    """Whether an added id is a regression rather than a flake.

    A flake fails at both revisions and no more often at one than the other; a
    regression fails at the delivered head more often than at the base.
    """
    return head_failures > base_failures


def _module_name(path: str) -> str:
    """The dotted module a changed ``.py`` path names, or "" when it names none."""
    if not path.endswith(".py"):
        return ""
    bare = path[: -len(".py")].replace("/", ".")
    return bare.removesuffix(".__init__")


def importer_population(changed: Iterable[str], tree: Path) -> tuple[str, ...]:
    """The test files importing each module the change touched.

    Only modules are considered: a test file the change touched is already
    covered by the suite pair, and importing itself would be meaningless. The
    population is returned sorted and repository-relative so two measurements
    of one tree name the same files in the same order.
    """
    from reckon.served_code import _edges

    modules = {
        name
        for name in (_module_name(str(path)) for path in changed)
        if name and not name.startswith("tests.")
    }
    if not modules:
        return ()
    tests_root = tree / "tests"
    if not tests_root.is_dir():
        return ()
    population: list[str] = []
    known = frozenset(modules)
    for path in sorted(tests_root.rglob("test_*.py")):
        relative = path.relative_to(tree).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            parsed = ast.parse(text, filename=relative)
        except (OSError, SyntaxError):
            # Let pytest report an unreadable caller instead of treating it as
            # proof that the file imports none of the changed modules.
            population.append(relative)
            continue
        imports = _edges(parsed, _module_name(relative), False, known)
        # A qualified name in a string can name an indirect import. Retain
        # those callers alongside the syntax tree's resolved import edges.
        if imports or any(module in text for module in modules):
            population.append(relative)
    return tuple(population)


def _target_name(token: str) -> str:
    """A command token with the path spelling that names the same file removed."""
    name = str(token).strip()
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def _distinct_files(paths: Iterable[str], exclude: Iterable[str] = ()) -> list[str]:
    """The files one command names, each counted once.

    A node's armed test is also an importer when it imports the module the node
    changed, and a delivery scope may name one file twice. A command that names
    it twice runs it a second time under the same bound, so every file is named
    once however many routes put it in the population. ``exclude`` names the
    files the command already carries, which need not be named again.
    """
    taken = {_target_name(token) for token in exclude}
    seen: dict[str, None] = {}
    for path in paths:
        name = _target_name(str(path))
        if name and name not in taken:
            seen.setdefault(name, None)
    return list(seen)


def armed_argv(
    command: str, test_files: Iterable[str] = ()
) -> tuple[dict[str, str], list[str]]:
    """Split an armed command into its environment and the program to run.

    The armed command is written the way a shell reads it, and two of its
    parts are not argv at all. Leading ``NAME=value`` tokens set the
    environment of the program instead of being the program, so running them
    as argv fails on the assignment itself. ``{node_test_files}`` stands for
    the node's own fenced test files, which the measurement is handed rather
    than left for the runner to interpret.

    Both are resolved here, once, so the suite pair, the importer population
    and the isolated re-runs all start the same program with the same
    environment.
    """
    tokens = shlex.split(command or "")
    environment: dict[str, str] = {}
    while tokens and _ASSIGNMENT.match(tokens[0]):
        name, _, value = tokens.pop(0).partition("=")
        environment[name] = value
    files = [str(path).strip() for path in test_files if str(path).strip()]
    argv: list[str] = []
    for token in tokens:
        if token == _SCOPED_PLACEHOLDER:
            argv.extend(files)
        else:
            argv.append(token)
    return environment, argv


def runner_argv(command: Sequence[str], test_files: Iterable[str] = ()) -> list[str]:
    """The armed command with the node's own test files removed.

    The armed command is what the run's promotion runs, so a single id is
    re-run through the same interpreter, environment, wrappers, plugins and
    options. Which tokens are its test targets is not guessed from their
    shape: they are exactly the node's test files the command was resolved
    from, so every other token — a ``timeout`` or ``env`` prefix, an option
    and its value — is kept and only the files are dropped.
    """
    targets = {_target_name(path) for path in test_files if str(path).strip()}
    return [token for token in command if _target_name(token) not in targets]


def isolated_command(
    command: Sequence[str], test_id: str, test_files: Iterable[str] = ()
) -> list[str]:
    """The armed command narrowed to exactly one test id."""
    return [*runner_argv(command, test_files), test_id]


def _carry_missing_tests(
    base_tree: Path, tree: Path, test_files: Iterable[str]
) -> None:
    """Copy the node's test files the base tree does not carry from the head.

    A test the node added does not exist at the base revision, so the base arm
    would stop on a missing file having measured nothing at all. The head's
    copy is what the base arm reads, which is what makes that file's failures
    countable as pre-existing rather than as an unreadable arm.

    A path that is already present is left untouched: at the base revision it
    is the revision's own file, and that is the file the base arm should read.
    """
    for relative in test_files:
        source = Path(tree) / relative
        target = Path(base_tree) / relative
        if not source.is_file() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError:
            # An unreadable copy leaves the arm to report the missing file,
            # which is a reading of the run rather than of the measurement.
            continue


def _tree_holds(tree: Path, relative: str) -> bool:
    """Whether one arm's own checkout holds the file this path names.

    A tree that is not there is not evidence of absence: dropping every file it
    would have run would turn a base or head checkout that failed to build into
    a clean empty measurement. The arm runs and reports its own failure.
    """
    root = Path(tree)
    try:
        if not root.is_dir():
            return True
        return (root / relative).is_file()
    except OSError:
        return True


def arm_test_files(
    test_files: Iterable[str], *, base_tree: Path, tree: Path
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """The test files each landing arm runs, and every file left out.

    A landing arm runs only those of the node's files the tree it measures
    holds. A file the base tree does not carry is new to the delivery, so it
    runs at the head that adds it and is left out of the base; a file the head
    tree does not carry was deleted, so it runs in neither arm even though the
    base still holds it. Naming a file a tree does not carry stops the whole
    arm on pytest's unmatched-pathspec error, which reads as an unreadable
    measurement rather than as the file's absence.

    Every file left out is returned with the arms it was left out of, so a
    reader sees what the pair did not measure.
    """
    base: list[str] = []
    head: list[str] = []
    left_out: dict[str, list[str]] = {}
    for relative in test_files:
        in_base = _tree_holds(base_tree, relative)
        in_head = _tree_holds(tree, relative)
        if in_head:
            head.append(relative)
        if in_base and in_head:
            base.append(relative)
        omitted = [
            arm for arm, held in (("base", in_base), ("head", in_head)) if not held
        ]
        if omitted:
            left_out[relative] = omitted
    return base, head, left_out


def _scoped_arm_argv(
    command: str, run_files: Sequence[str], all_files: Sequence[str]
) -> list[str]:
    """One arm's command, resolved to the test files its own tree holds.

    A scoped command takes the arm's own files in the placeholder's place. A
    declaration naming its test files literally keeps only those this arm's
    tree holds, so an arm never hands the runner a path it cannot open.
    """
    _environment, argv = armed_argv(command, run_files)
    if _SCOPED_PLACEHOLDER in (command or ""):
        return argv
    omitted = [name for name in all_files if name not in set(run_files)]
    return runner_argv(argv, omitted)


def _names_test(run_files: Iterable[str], test_id: str) -> bool:
    """Whether one arm's file list covers the file a test id belongs to."""
    return _target_name(test_id.partition("::")[0]) in set(run_files)


def _command_text(environment: Mapping[str, str], argv: Sequence[str]) -> str:
    """One arm's command as the shell text the arm ran and its log names."""
    assignments = [f"{name}={value}" for name, value in environment.items()]
    return shlex.join([*assignments, *argv])


def _empty_arm(command: Sequence[str]) -> Arm:
    """An arm whose own tree holds none of the files it would have run.

    A scoped command whose placeholder resolves to nothing would start the
    whole suite, so the arm is not run at all. It records no failure because no
    test could fail, which is the same reading an empty file list states.
    """
    return Arm(tuple(command), 0, (), "")


def _link_environment(repo: Path, tree: Path) -> None:
    """Give a base tree the ``.venv`` link a provisioned worktree carries.

    An armed command that names the project's interpreter by a relative path —
    ``.venv/bin/python`` — starts it in the base tree only when the link the
    fleet provisions into every worker worktree is there too. The link points
    at the repository's own environment, resolved through any link the
    repository itself carries, so a worktree and its main checkout share one.

    A repository with no environment is left alone: the arms then fail with
    the command's own error, which the measurement records as unreadable.
    """
    from reckon.crew.fleet_supervisor import resolved_environment

    environment = resolved_environment(repo)
    if not environment.exists():
        return
    link = tree / ".venv"
    if link.exists() or link.is_symlink():
        return
    with contextlib.suppress(OSError):
        link.symlink_to(environment)


def measure_timeout(config: Mapping[str, Any] | None) -> float:
    """The wall-clock bound for one measurement subprocess, in seconds.

    Read from ``landing.measure_timeout_seconds`` in resolved flight config, so
    a host can bound a slow project's suite without a code change. A config
    that declares no such key, or a value that is not a positive number, falls
    back to the shipped default rather than removing the bound.
    """
    landing = (config or {}).get("landing")
    value = (
        landing.get("measure_timeout_seconds", DEFAULT_MEASURE_TIMEOUT_SECONDS)
        if isinstance(landing, Mapping)
        else DEFAULT_MEASURE_TIMEOUT_SECONDS
    )
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_MEASURE_TIMEOUT_SECONDS
    return float(value)


#: The longest wall clock each partition admits, in seconds, with ``None``
#: meaning the partition admits any length. A partition declared in neither this
#: table nor ``landing.partition_max_seconds`` is unbounded, because an unread
#: ceiling cannot justify refusing a declaration.
DEFAULT_PARTITION_MAX_SECONDS: dict[str, float | None] = {
    "all_debug": 3600.0,
    "rigel": None,
}

_UNBOUNDED_SPELLINGS = frozenset({"unlimited", "unbounded", "none", "infinite"})

_UNSET = object()


def parse_gate_duration(value: Any) -> float | None:
    """A declared gate duration in seconds, or ``None`` when it cannot be read.

    A duration is written the way a scheduler states one — ``01:15:00`` — or as
    a number with an ``s``/``m``/``h`` suffix, either a number of seconds, an
    unsuffixed count, ``900``. Anything else is unread, so a malformed
    declaration is refused rather than quietly treated as no duration at all.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    clock = re.fullmatch(r"(\d+):([0-5]\d)(?::([0-5]\d))?", text)
    if clock:
        hours = int(clock.group(1))
        minutes = int(clock.group(2))
        seconds = int(clock.group(3) or 0)
        total = hours * 3600 + minutes * 60 + seconds
        return float(total) if total > 0 else None
    suffixed = re.fullmatch(r"(\d+)\s*([smh])", text, re.IGNORECASE)
    if suffixed:
        amount = int(suffixed.group(1))
        total = amount * {"s": 1, "m": 60, "h": 3600}[suffixed.group(2).lower()]
        return float(total) if total > 0 else None
    plain = re.fullmatch(r"\d+", text)
    if plain:
        total = int(text)
        return float(total) if total > 0 else None
    return None


def partition_max_seconds(
    partition: str,
    config: Mapping[str, Any] | None = None,
) -> float | None:
    """The longest wall clock one partition admits, in seconds.

    Read from the config key ``landing.partition_max_seconds``, a mapping of
    partition name to a number of seconds or an unbounded spelling. A partition
    the config does not name is read from the shipped table, and one neither
    names is unbounded: an unknown ceiling admits, it never refuses. A ceiling
    the config does name but whose value cannot be read is refused, because a
    typo there must not quietly remove the limit the declaration is checked
    against.
    """
    landing = (config or {}).get("landing")
    declared = (
        landing.get("partition_max_seconds") if isinstance(landing, Mapping) else None
    )
    if isinstance(declared, Mapping) and partition in declared:
        raw = declared[partition]
        if raw is None or (
            isinstance(raw, str) and raw.strip().lower() in _UNBOUNDED_SPELLINGS
        ):
            return None
        seconds = parse_gate_duration(raw)
        if seconds is None:
            raise ValueError(
                f"landing.partition_max_seconds[{partition!r}] is {raw!r}, which "
                "is not a duration; write seconds (3600), a clock (01:00:00), a "
                "suffix (1h), or one of " + ", ".join(sorted(_UNBOUNDED_SPELLINGS))
            )
        return seconds
    return DEFAULT_PARTITION_MAX_SECONDS.get(partition)


def admitting_partition(
    bound_seconds: float,
    partition: str,
    long_partition: str,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """The partition a bound is submitted on, or ``None`` when none admits it.

    A bound longer than the short partition's maximum moves to the long one; a
    bound longer than both is one no configured partition admits, so the caller
    refuses rather than submitting a job the scheduler would end mid-run.
    """
    candidates = (partition, long_partition or "rigel")
    for name in candidates:
        if not name:
            continue
        maximum = partition_max_seconds(name, config)
        if maximum is None or bound_seconds <= maximum:
            return name
    return None


def _arm_records(record: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Every arm one stored measurement carries, in no particular order."""
    for section in ("suite", "population"):
        container = record.get(section)
        if not isinstance(container, Mapping):
            continue
        for name in ("base", "head"):
            arm = container.get(name)
            if isinstance(arm, Mapping):
                yield arm


def seconds_per_file(record: Mapping[str, Any]) -> list[float]:
    """Each arm of one stored measurement as its seconds per file read.

    An arm that recorded no elapsed time or no file count says nothing about a
    rate and is left out, so a record written before the pair was kept never
    contributes a division by zero or a fabricated zero rate.
    """
    rates: list[float] = []
    for arm in _arm_records(record):
        elapsed = arm.get("elapsed_seconds")
        count = arm.get("file_count")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            continue
        if isinstance(count, bool) or not isinstance(count, (int, float)) or count <= 0:
            continue
        if elapsed <= 0:
            continue
        rates.append(float(elapsed) / float(count))
    return rates


def median_seconds_per_file(records: Iterable[Mapping[str, Any]]) -> float | None:
    """A project's median seconds per file across its stored measurements.

    The median rather than the mean, because one arm that happened to run while
    the host was saturated is exactly the reading that must not set every later
    bound. ``None`` when no stored measurement carries a rate at all, which is
    what a project with no history has.
    """
    rates: list[float] = []
    for record in records:
        if isinstance(record, Mapping):
            rates.extend(seconds_per_file(record))
    if not rates:
        return None
    return statistics.median(rates)


def arm_bound(
    floor: float,
    file_count: int,
    seconds_per_file: float | None,
    multiple: float = BOUND_MULTIPLE,
) -> float:
    """The wall-clock bound one arm runs under, in seconds.

    The larger of the configured floor and ``multiple`` times the arm's file
    count times the project's median seconds per file, so an arm whose
    population last took 900 s is given a bound above 900 s rather than a fixed
    figure that ignores its own history. An arm with no history — or one that
    names no files — is bounded by the floor, which is the only reading such an
    arm leaves.
    """
    if seconds_per_file is None or seconds_per_file <= 0 or file_count <= 0:
        return float(floor)
    return max(float(floor), float(multiple) * float(file_count) * seconds_per_file)


def _arm(
    command: Sequence[str],
    tree: Path,
    log_path: Path,
    run,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
    file_count: int = 0,
) -> Arm:
    """Run one command at one tree and record its failure reported ids.

    ``environment`` is the armed command's own leading assignments, layered
    over the measurement's environment; a caller that names none inherits the
    parent's. ``file_count`` is how many files this arm's population reads,
    which with the elapsed time is what a later arm's bound is sized from.

    The command is bounded: a run that exceeds ``timeout`` is recorded as
    unreadable and no failure id is taken from whatever it had printed, because
    a partial log does not say whether the id after it would have failed. A
    timed-out arm still records the seconds it was given, which is the reading
    that widens the next bound.
    """
    argv = list(command)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A command that declares no environment is run exactly as before: the
    # caller's own environment is inherited, unread and unrewritten.
    extra = {"env": {**os.environ, **environment}} if environment else {}
    if getattr(run, "partitioned", False):
        extra["arm_log_path"] = str(log_path)
        extra["arm_environment"] = environment or {}
    partitioned = getattr(run, "partitioned", False)
    handle = (
        contextlib.nullcontext(None)
        if partitioned
        else log_path.open("x", encoding="utf-8")
    )
    with handle as stream:
        started = time.monotonic()
        try:
            finished = run(
                argv,
                cwd=str(tree),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                **extra,
            )
        except subprocess.TimeoutExpired as expired:
            elapsed = time.monotonic() - started
            partial = f"{expired.stdout or ''}{expired.stderr or ''}"
            if stream is not None:
                stream.write(f"timed out after {timeout}s\n{partial}")
            return Arm(
                tuple(argv),
                None,
                (),
                str(log_path),
                timed_out=True,
                elapsed_seconds=elapsed,
                file_count=file_count,
            )
        except OSError as exc:
            elapsed = time.monotonic() - started
            if stream is not None:
                stream.write(f"{type(exc).__name__}: {exc}\n")
            return Arm(
                tuple(argv),
                None,
                (),
                str(log_path),
                unreadable_reason=f"{type(exc).__name__}: {exc}",
                elapsed_seconds=elapsed,
                file_count=file_count,
            )
        elapsed = time.monotonic() - started
        output = f"{finished.stdout or ''}{finished.stderr or ''}"
        if stream is not None:
            stream.write(output)
            if output and not output.endswith("\n"):
                stream.write("\n")
            stream.write(f"EXIT={finished.returncode}\n")
        return Arm(
            tuple(argv),
            int(finished.returncode),
            failing_ids(output),
            str(log_path),
            unreadable_reason=str(getattr(finished, "unreadable_reason", "") or ""),
            submission_failed=bool(getattr(finished, "submission_failed", False)),
            elapsed_seconds=elapsed,
            file_count=file_count,
            # A partitioned runner that read the scheduler's own cancel line
            # reports the timeout here; an arm that simply exceeded the
            # client-side ceiling is reported by the branch above.
            timed_out=bool(getattr(finished, "timed_out", False)),
            job_id=str(getattr(finished, "job_id", "") or ""),
            time_limit=str(getattr(finished, "time_limit", "") or ""),
        )


def partition_for_bound(
    partition: str,
    long_partition: str,
    bound_seconds: float,
    config: Mapping[str, Any] | None = None,
) -> str:
    """The partition a measure runs on, given its bound.

    The first of the measure partition and the long partition whose declared
    maximum admits the bound, so the partition a declaration is checked against
    and the partition its arm is submitted to are one decision. The shipped
    table encodes the hour rule: a bound past an hour takes the long partition,
    because the short partitions end a job that outlives them rather than
    letting it finish. A bound no configured partition admits still goes to the
    long one, so the scheduler's own refusal is the visible outcome rather than
    a silent swap to a partition the measure did not name.
    """
    return (
        admitting_partition(bound_seconds, partition, long_partition, config)
        or long_partition
        or "rigel"
    )


def time_limit_text(bound_seconds: float) -> str:
    """A scheduler time limit as the ``HH:MM:SS`` a submission carries."""
    seconds = max(1, math.ceil(bound_seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def partition_submission(
    *,
    script: str,
    partition: str,
    long_partition: str,
    bound_seconds: float,
    chdir: Path,
    log_path: Path,
    config: Mapping[str, Any] | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """The one batch submission form every partitioned measure uses.

    The argv and the environment are built here and nowhere else, so a caller
    submitting to a measure partition cannot drift from the form the landing
    measure uses. ``sbatch --wait`` submits a job of its own and blocks until
    that job ends; the job runs the script as an ``srun`` step inside itself,
    because a bare ``srun`` from the fleet node stays inside the fleet
    allocation whatever partition it names. The job's ``--time`` is the
    caller's bound rendered as ``HH:MM:SS``, so the scheduler ends an overrun
    rather than leaving it to a client-side timeout, and the caller's
    scheduler variables are removed so the job is not attached to whatever
    allocation the submission came from.

    The selected partition is returned beside them, because a caller that
    reports where its measure ran needs the partition the bound chose rather
    than the one that was declared for short bounds.
    """
    log_path = Path(log_path).absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve both outputs before submission; the task appends only to this run's files.
    with log_path.open("x", encoding="utf-8"):
        pass
    with Path(f"{log_path}.scheduler").open("x", encoding="utf-8"):
        pass
    selected = partition_for_bound(partition, long_partition, bound_seconds, config)
    bound = time_limit_text(bound_seconds)
    step = (
        shlex.join(
            [
                "srun",
                f"--partition={selected}",
                f"--time={bound}",
                "--ntasks=1",
                "bash",
                "-c",
                script,
            ]
        )
        + " >> "
        + shlex.quote(str(log_path))
        + " 2>&1"
    )
    argv = [
        "sbatch",
        "--wait",
        "--parsable",
        "--open-mode=append",
        f"--partition={selected}",
        f"--time={bound}",
        "--nodes=1",
        "--ntasks=1",
        f"--cpus-per-task={POPULATION_WORKERS}",
        "--mem=32G",
        f"--output={log_path}.scheduler",
        f"--error={log_path}.scheduler",
        f"--chdir={chdir}",
        "--wrap",
        step,
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SLURM_", "SBATCH_"))
    }
    environment["RECKON_ALLOW_NESTED_LAUNCH"] = "1"
    environment["TMPDIR"] = NODE_LOCAL_TEMP_ROOT
    return selected, argv, environment


def _delivered_head(tree: Path) -> str:
    """The revision a delivered worktree is at, when it can be read.

    The head of the tree a run delivered is the revision that holds the test
    files the run added, so it is what a measurement tree that lacks one of
    them carries it from. An unreadable head leaves the caller to fall back to
    the revision it already has, and the arm then refuses naming what it could
    not find.

    The read is the repository's shared commit resolver's, so a bound or a
    revision validation fixed there reaches this caller too rather than
    leaving a second reader to disagree with it.
    """
    from reckon.crew.recovery import _resolve_commit

    return _resolve_commit(tree, "HEAD")


def _carry_line(git: str, relative: str, revision: str) -> str:
    """One shell step carrying a test file the measurement tree lacks.

    The archive is named so a failure says which path was asked for and which
    revision it was asked of, rather than letting ``git archive`` and ``tar``
    each print a fragment of the same failure into the arm's log. A path the
    revision does not hold cannot be carried, so the tree cannot be built and
    the arm stops with that sentence instead of running a suite over a tree
    that is missing a file the node declared.
    """
    quote = shlex.quote
    return (
        f'if [ ! -e "$scratch/tree"/{quote(relative)} ]; then '
        f"{{ {git} archive {quote(revision)} -- {quote(relative)} | "
        'tar -x -C "$scratch/tree"; } || { '
        'printf "cannot build the measurement tree: %s is not present at '
        f'revision %s\\n" {quote(relative)} {quote(revision)} >&2; '
        "exit 4; }; fi"
    )


def _partition_runner(
    runner,
    *,
    repo: Path,
    base_tree: Path,
    base_sha: str,
    head_sha: str,
    partition: str,
    long_partition: str,
    test_files: Iterable[str],
    config: Mapping[str, Any] | None = None,
):
    """Submit each arm as an independent job with its own revision checkout."""
    common = _git(
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        timeout=DEFAULT_MEASURE_TIMEOUT_SECONDS,
    )
    if common.returncode:
        raise BaseCheckoutError("the shared git directory could not be read")
    git_dir = Path(common.stdout.strip())
    shared_repo = git_dir.parent
    from reckon.crew.fleet_supervisor import resolved_environment

    environment = resolved_environment(repo)
    carried = tuple(test_files)

    def run(command, **options):
        revision = base_sha if Path(options["cwd"]) == base_tree else head_sha
        timeout = options["timeout"]
        log = Path(options.pop("arm_log_path")).absolute()
        scratch_id = hashlib.sha256(str(log).encode()).hexdigest()[:12]
        quote = shlex.quote
        git = shlex.join(["git", "--git-dir", str(git_dir)])
        carry = "\n".join(_carry_line(git, relative, head_sha) for relative in carried)
        assignments = options.pop("arm_environment")
        invocation = shlex.join(
            ["env", *[f"{k}={v}" for k, v in assignments.items()], *command]
        )
        script = f"""set -euo pipefail
scratch=
trap 'status=$?; if [ -n "$scratch" ]; then rm -rvf "$scratch"; fi; printf "EXIT=%s\n" "$status"' EXIT
export TMPDIR={NODE_LOCAL_TEMP_ROOT}
scratch=$(mktemp -d "{NODE_LOCAL_TEMP_ROOT}/$USER-landing-{scratch_id}-XXXXXX")
# A scheduler that ends the job by signal still releases the tree: the TERM
# handler exits, which is what runs the EXIT trap above.
trap 'exit 143' TERM INT
export TMPDIR="$scratch"
mkdir "$scratch/tree"
{git} archive {quote(revision)} | tar -x -C "$scratch/tree"
git init -q "$scratch/tree"
printf '%s\n' {quote(str(git_dir / "objects"))} > "$scratch/tree/.git/objects/info/alternates"
git -C "$scratch/tree" update-ref HEAD {quote(revision)}
git -C "$scratch/tree" read-tree {quote(revision)}
{carry}
ln -s {quote(str(environment))} "$scratch/tree/.venv"
cd "$scratch/tree"
printf '# revision: %s\n# host: %s\n# tree: %s\n' {quote(revision)} "$(hostname)" "$PWD"
printf '# command: %s\n' {quote(invocation)}
set +e
{invocation}
status=$?
exit "$status"
"""
        _selected, argv, env = partition_submission(
            script=script,
            partition=partition,
            long_partition=long_partition,
            bound_seconds=timeout,
            chdir=shared_repo,
            log_path=log,
            config=config,
        )
        try:
            finished = runner(
                argv,
                cwd=str(shared_repo),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout + 120,
                env=env,
            )
        except subprocess.TimeoutExpired as expired:
            # sbatch --wait prints the id before waiting; cancel only that job.
            output = expired.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            match = re.match(r"^(\d+)(?:;[^\n]+)?(?:\n|$)", output)
            if match:
                with log.open("a") as stream:
                    stream.write(f"cancelling owned measurement job {match.group(1)}\n")
                cancelled = runner(
                    ["scancel", match.group(1)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                    env=env,
                )
                with log.open("a") as stream:
                    stream.write(
                        f"cancelled job {match.group(1)}: {cancelled.returncode}\n"
                    )
                    stream.write(f"{cancelled.stdout or ''}{cancelled.stderr or ''}")
            raise
        output = log.read_text() if log.exists() else ""
        limit = time_limit_text(timeout)
        job_id = ""
        printed = re.match(
            r"^(\d+)(?:;[^\n]*)?(?:\n|$)", (finished.stdout or "").strip()
        )
        if printed:
            job_id = printed.group(1)
        # The scheduler's own cancel line is the account of an arm stopped at
        # the job's time limit: the trap that could have printed a receipt was
        # killed with everything else, so the log has no receipt and nothing
        # the arm printed is complete. The job's limit is the bound the
        # submission carried, and the refusal names both.
        timed_out = bool(_TIME_LIMIT_CANCEL.search(output))
        if timed_out:
            named = _SCHEDULER_JOB.search(output)
            if named:
                job_id = job_id or named.group(1)
        # Only the arm receipt can report a test exit. A failed submission or a
        # job killed before its receipt is unreadable even if stdout looks green.
        # The scheduler appends lines of its own to the job's log after the
        # arm's trap has printed the receipt, so the receipt is read on its own
        # line rather than as the log's last one; the last such line is the
        # trap's, since the trap prints after everything the arm itself ran.
        receipts = _EXIT_RECEIPT.findall(output)
        reason = ""
        refusal = _TREE_BUILD_REFUSAL.search(output)
        # An arm whose submission never started a job leaves its log empty: the
        # job writes the log, so anything in it is evidence a job ran, whether
        # or not its trap printed a receipt. Without that evidence, and with no
        # job id printed before the wait, the submission is what failed, and
        # what refused it is the only thing worth recording.
        submission_failed = (
            not timed_out
            and bool(finished.returncode)
            and not job_id
            and not output.strip()
        )
        status = (
            int(receipts[-1])
            if receipts and not timed_out and not submission_failed
            else 3
        )
        if timed_out:
            reason = (
                f"the scheduler cancelled job {job_id or 'unknown'} at its "
                f"{limit} time limit"
            )
        elif submission_failed:
            spoken = f"{finished.stdout or ''}{finished.stderr or ''}".strip()
            reason = (
                "its submission never started a scheduler job: "
                f"{shlex.join(str(token) for token in argv)} exited "
                f"{finished.returncode} with {spoken or 'no output'}"
            )
        elif refusal:
            # The arm's own sentence names the path and the revision, so it is
            # the reason a reader is given rather than a log excerpt.
            reason = refusal.group(0).strip()
        elif not receipts:
            reason = "carries no exit receipt"
        elif status != finished.returncode:
            reason = (
                f"reported exit {status} in its receipt while the job returned "
                f"{finished.returncode}"
            )
            status = 3
        returned = subprocess.CompletedProcess(
            command,
            status,
            output or f"{finished.stdout or ''}{finished.stderr or ''}",
            "",
        )
        returned.unreadable_reason = reason
        returned.submission_failed = submission_failed
        returned.job_id = job_id
        returned.timed_out = timed_out
        returned.time_limit = limit
        return returned

    return run


def measure_run(
    *,
    command: str,
    tree: Path,
    base_sha: str,
    head_sha: str,
    base_tree: Path,
    logs_dir: Path,
    test_files: Iterable[str] = (),
    run=None,
    repeats: int = REPEATS,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
    stored: Iterable[Mapping[str, Any]] = (),
    project: str = "",
    changed: Iterable[str] | None = None,
    config: Mapping[str, Any] | None = None,
    population_files: Iterable[str] | None = None,
    repo: Path | None = None,
    declared_duration: float | None = None,
    declared_partition: str = "",
    declared_long_partition: str = "",
) -> Measurement:
    """Run the three readings for one delivered run and return the verdict.

    ``test_files`` are the node's own test files, the ones the armed command
    was resolved from; they are what a caller's single-id re-run replaces.
    ``timeout`` is the floor every arm's bound is at least, and ``stored`` is
    the project's stored measurements, whose arms size each bound above that
    floor from how long this project's populations actually take. ``run`` is
    the command runner, ``subprocess.run``-shaped, so a caller can drive the
    measurement without executing a real suite. ``changed`` optionally names
    the run's own changed modules independently of the compared revisions;
    the landing caller compares current primary with a merge candidate while
    selecting importers of the delivered change. ``population_files`` names an
    importer population a caller already read from a tree it has since
    released, so a measurement whose arms run elsewhere need not hold a tree on
    this node to read what files they run. ``declared_duration`` is the
    duration a run's own gate declared, and it raises the floor every arm runs
    above for that run alone: a gate that legitimately takes 54 minutes lands
    under a bound that admits it, while every other run in the project keeps
    the project's floor. ``declared_partition`` and ``declared_long_partition``
    let a run whose arms are only comparable on one partition measure there.
    """
    runner = run or _RUN
    if config is None:
        from reckon import flight

        config = flight.resolve(project or None, checkout_path=str(tree)).config
    landing = config.get("landing") or {}
    partition = str(declared_partition or landing.get("measure_partition") or "")
    long_partition = str(
        declared_long_partition or landing.get("measure_long_partition") or "rigel"
    )
    execution = "slurm" if partition else "in-place"
    # A project that declares its measurement limit owns the floor every arm
    # runs above: a caller that omits the figure, or passes a smaller one, must
    # not submit an arm under a ceiling the project has said is too short for
    # its own populations. A run's declared gate duration raises that floor for
    # this run: the gate has measured how long it takes, and an arm bounded
    # below it is ended mid-run by the scheduler rather than read.
    timeout = max(
        float(timeout),
        measure_timeout(config),
        float(declared_duration or 0.0),
    )
    node_tests = _distinct_files(
        str(path).strip() for path in test_files if str(path).strip()
    )
    rate = median_seconds_per_file(stored)

    def bound(file_count: int) -> float:
        return arm_bound(timeout, file_count, rate)

    environment, argv = armed_argv(command, node_tests)
    if not argv:
        return Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the run armed no suite command",
            head=head_sha,
            project=project,
        )
    if _SCOPED_PLACEHOLDER in (command or "") and not node_tests:
        return Measurement(
            command=command,
            suite={},
            population={},
            unreadable=(
                "the armed command measures the node's own test files, "
                "and the node names none"
            ),
            head=head_sha,
            project=project,
        )
    files = (
        tuple(str(name) for name in population_files)
        if population_files is not None
        else importer_population(
            changed_paths(tree, base_sha, head_sha) if changed is None else changed,
            tree,
        )
    )
    if partition:
        runner = _partition_runner(
            runner,
            repo=repo or tree,
            base_tree=base_tree,
            base_sha=base_sha,
            head_sha=head_sha,
            partition=partition,
            long_partition=long_partition,
            # A partitioned arm builds its own tree from its own revision, so
            # it already holds every file that revision carries. Carrying the
            # head's copies into the base arm is what made a base arm run a
            # file its own revision does not have.
            test_files=[],
            config=config,
        )
        runner.partitioned = True
    scoped = _SCOPED_PLACEHOLDER in (command or "")
    base_tests, head_tests, left_out = arm_test_files(
        node_tests, base_tree=base_tree, tree=tree
    )
    base_argv = _scoped_arm_argv(command, base_tests, node_tests)
    head_argv = _scoped_arm_argv(command, head_tests, node_tests)
    from reckon.crew.promotion_gate import _unused_log_path

    logs = Path(logs_dir)
    base_suite_bound = bound(len(base_tests))
    head_suite_bound = bound(len(head_tests))
    suite_base = (
        _arm(
            base_argv,
            base_tree,
            _unused_log_path(logs / "suite-base.log"),
            runner,
            base_suite_bound,
            environment,
            file_count=len(base_tests),
        )
        if base_tests or not scoped
        else _empty_arm(base_argv)
    )
    suite_head = (
        _arm(
            head_argv,
            tree,
            _unused_log_path(logs / "suite-head.log"),
            runner,
            head_suite_bound,
            environment,
            file_count=len(head_tests),
        )
        if head_tests or not scoped
        else _empty_arm(head_argv)
    )
    # The population runs the armed command extended with the importers, so an
    # importer the armed command already names is not named again: a file run
    # twice reads the same tests twice under one bound. Each arm's own tree
    # decides which importers it runs, exactly as for the node's own files.
    pop_base_tests, pop_head_tests, _population_left_out = arm_test_files(
        files, base_tree=base_tree, tree=tree
    )
    pop_base_files = _distinct_files(pop_base_tests, exclude=base_argv)
    pop_head_files = _distinct_files(pop_head_tests, exclude=head_argv)
    population_base_command = [
        *base_argv,
        *pop_base_files,
        "-n",
        str(POPULATION_WORKERS),
    ]
    population_head_command = [
        *head_argv,
        *pop_head_files,
        "-n",
        str(POPULATION_WORKERS),
    ]
    population_base_bound = bound(len(pop_base_tests))
    population_head_bound = bound(len(pop_head_tests))
    population_base_runs = bool(pop_base_files or base_tests or not scoped)
    population_head_runs = bool(pop_head_files or head_tests or not scoped)
    if files:
        population_base = (
            _arm(
                population_base_command,
                base_tree,
                _unused_log_path(logs / "population-base.log"),
                runner,
                population_base_bound,
                environment,
                file_count=len(pop_base_tests),
            )
            if population_base_runs
            else _empty_arm(population_base_command)
        )
        population_head = (
            _arm(
                population_head_command,
                tree,
                _unused_log_path(logs / "population-head.log"),
                runner,
                population_head_bound,
                environment,
                file_count=len(pop_head_tests),
            )
            if population_head_runs
            else _empty_arm(population_head_command)
        )
    else:
        population_base = Arm(tuple(population_base_command), None, (), "")
        population_head = Arm(tuple(population_head_command), None, (), "")
    # Every file one arm ran, so an added failure from either the suite or the
    # importer population is re-run where it was seen and only a file the arm
    # never named is left without a reading.
    base_run_files = [*base_tests, *pop_base_files]
    head_run_files = [*head_tests, *pop_head_files]
    suite = {
        "command": command,
        "base": suite_base.as_dict(),
        "head": suite_head.as_dict(),
        "left_out": left_out,
    }
    population = {
        "files": list(files),
        "base": population_base.as_dict(),
        "head": population_head.as_dict(),
    }
    unreadable = ""
    judged: list[tuple[str, Arm, float]] = []
    if base_tests or not scoped:
        judged.append(("base", suite_base, base_suite_bound))
    if head_tests or not scoped:
        judged.append(("head", suite_head, head_suite_bound))
    if files:
        if population_base_runs:
            judged.append(("population base", population_base, population_base_bound))
        if population_head_runs:
            judged.append(("population head", population_head, population_head_bound))
    for name, arm, arm_bound_seconds in judged:
        if not arm.readable:
            if arm.timed_out:
                # The job and its limit are named inside the guard so a reader
                # learns which submitted arm the scheduler stopped; the guard
                # still ends on its bound, which is what the landing retries on.
                scheduler = ""
                if arm.job_id or arm.time_limit:
                    job = f"job {arm.job_id}" if arm.job_id else "its job"
                    limit = f"limit {arm.time_limit}" if arm.time_limit else "limit"
                    scheduler = f" (scheduler {job}, {limit})"
                unreadable = (
                    f"the {name} arm{scheduler} exceeded its "
                    f"{arm_bound_seconds:g}s bound"
                )
            elif arm.submission_failed:
                # The arm never reached the scheduler, so the reason is the
                # submission's own, not a bare absence: an arm that ends in
                # hundredths of a second with an empty log says nothing a
                # reader can act on unless its submission is quoted.
                unreadable = (
                    f"the {name} arm never reached the scheduler: "
                    f"{arm.unreadable_reason}"
                )
            elif arm.unreadable_reason and arm.exit_status is None:
                # A runner that could not be launched at all (a too-long argv,
                # a missing program) leaves no status, no job, no id and no
                # log, so the absence is again all a bare "did not run" could
                # report. The launch failure is the only fact the arm
                # recorded, and it is the one a reader can act on.
                unreadable = (
                    f"the {name} arm could not be launched: "
                    f"{arm.unreadable_reason}"
                )
            else:
                unreadable = f"the {name} arm did not run"
            break
    added_base = {*suite_base.failure_ids, *population_base.failure_ids}
    added_head = [*suite_head.failure_ids, *population_head.failure_ids]
    uncovered = failure_ids_covered(added_base, added_head)
    added = list(
        dict.fromkeys(test_id for test_id in added_head if test_id in uncovered)
    )
    isolated: dict[str, dict[str, list[bool]]] = {}

    def narrowed(test_id: str, arm_tree: Path, arm: str, n: int) -> tuple[bool, str]:
        run_files, arm_argv = (
            (base_run_files, base_argv)
            if arm == "base"
            else (head_run_files, head_argv)
        )
        if not _names_test(run_files, test_id):
            # This arm's tree does not carry the id's file, so it never ran the
            # test: no reading of it exists here, rather than a failed one.
            return False, ""
        return _fails(
            isolated_command(arm_argv, test_id, run_files),
            test_id,
            arm_tree,
            _unused_log_path(logs / "isolated" / f"{_slug(test_id)}-{arm}-{n}.log"),
            runner,
            bound(1),
            environment,
            file_count=1,
        )

    for test_id in added:
        results = {"base": [], "head": []}
        for arm, arm_tree in (("base", base_tree), ("head", tree)):
            for n in range(1, repeats + 1):
                failed, problem = narrowed(test_id, arm_tree, arm, n)
                if problem and not unreadable:
                    unreadable = problem
                results[arm].append(failed)
        isolated[test_id] = results
    real = tuple(
        test_id
        for test_id, results in isolated.items()
        if _is_real(sum(results["base"]), sum(results["head"]))
    )
    return Measurement(
        command=command,
        suite=suite,
        population=population,
        isolated=isolated,
        execution=execution,
        real=real,
        unreadable=unreadable,
        head=head_sha,
        project=project,
    )


def arm_observation(
    command: str,
    revision: str,
    arm: Arm,
    *,
    measured_by: str = "landing",
    left_out: Iterable[str] = (),
) -> dict[str, Any]:
    """One arm as the suite-observation record a run's manifest carries.

    The shape is the one promotion already reads for ``baseline_suite`` and
    ``after_suite``, so an arm the landing measured is judged by the same code
    and the same refusal as an arm the run recorded. ``left_out`` names the
    files this arm's tree does not hold, so a reader sees what the arm did not
    run rather than inferring it from a shorter command.
    """
    observation = {
        "revision": revision,
        "command": command,
        "exit_status": arm.exit_status,
        "completed": arm.exit_status in (0, 1),
        "failure_count": len(arm.failure_ids),
        "failure_ids": list(arm.failure_ids),
        "log_path": arm.log_path,
        "measured_by": measured_by,
    }
    named = list(left_out)
    if named:
        observation["left_out"] = named
    return observation


@_handoff_measurement
def armed_observations(
    *,
    command: str,
    tree: Path,
    base_tree: Path,
    base_sha: str,
    head_sha: str,
    logs_dir: Path,
    test_files: Iterable[str] = (),
    run=None,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
    project: str = "",
    config: Mapping[str, Any] | None = None,
    partition: str = "",
    long_partition: str = "",
    declared_duration: float | None = None,
) -> dict[str, Any]:
    """Run the armed command at both revisions and return each as an observation.

    A run whose recorded arms are missing, were recorded at another revision, or
    name another command has not produced the evidence its own landing needs, so
    the landing takes the reading itself: the armed command is what promotion
    runs, and the arms it produces here are that evidence, taken at the base and
    the head the landing needs rather than at whatever the run happened to
    record. Each arm carries ``measured_by: landing`` so a later reader can tell
    it from one the run recorded.

    A configured measurement partition submits each arm as its own job, the same
    way :func:`measure_run` does, so a mismatch costs a queue wait rather than a
    suite run on the node the landing is executing on. The return value carries
    ``unreadable`` instead of a pair when the command cannot be measured at all.
    """
    runner = run or _RUN
    node_tests = [str(path).strip() for path in test_files if str(path).strip()]
    environment, argv = armed_argv(command, node_tests)
    if not argv:
        return {"unreadable": "the run armed no suite command"}
    if _SCOPED_PLACEHOLDER in (command or "") and not node_tests:
        return {
            "unreadable": (
                "the armed command measures the node's own test files, "
                "and the node names none"
            )
        }
    if config is None:
        from reckon import flight

        config = flight.resolve(project or None, checkout_path=str(tree)).config
    landing = config.get("landing") or {}
    # A run whose gate declared its partition measures there, ahead of the
    # project's own measure partition: the arms were only comparable on the
    # partition the declaration names, so a measurement anywhere else would
    # re-ask a question the declaration already answers.
    partition = str(partition or landing.get("measure_partition") or "")
    long_partition = str(
        long_partition or landing.get("measure_long_partition") or "rigel"
    )
    timeout = max(
        float(timeout),
        measure_timeout(config),
        float(declared_duration or 0.0),
    )
    if partition:
        runner = _partition_runner(
            runner,
            repo=tree,
            base_tree=base_tree,
            base_sha=base_sha,
            head_sha=head_sha,
            partition=partition,
            long_partition=long_partition,
            # A partitioned arm builds its own tree from its own revision, so
            # it already holds every file that revision carries. Carrying the
            # head's copies into the base arm is what made a base arm run a
            # file its own revision does not have.
            test_files=[],
            config=config,
        )
        runner.partitioned = True
    scoped = _SCOPED_PLACEHOLDER in (command or "")
    base_tests, head_tests, left_out = arm_test_files(
        node_tests, base_tree=base_tree, tree=tree
    )
    base_argv = _scoped_arm_argv(command, base_tests, node_tests)
    head_argv = _scoped_arm_argv(command, head_tests, node_tests)
    from reckon.crew.promotion_gate import _unused_log_path

    logs = Path(logs_dir)
    base_arm = (
        _arm(
            base_argv,
            base_tree,
            _unused_log_path(logs / "armed-baseline.log"),
            runner,
            timeout,
            environment,
            file_count=len(base_tests),
        )
        if base_tests or not scoped
        else _empty_arm(base_argv)
    )
    head_arm = (
        _arm(
            head_argv,
            tree,
            _unused_log_path(logs / "armed-after.log"),
            runner,
            timeout,
            environment,
            file_count=len(head_tests),
        )
        if head_tests or not scoped
        else _empty_arm(head_argv)
    )
    unreadable = ""
    for name, arm in (("baseline", base_arm), ("after", head_arm)):
        if not arm.readable and not unreadable:
            unreadable = (
                f"the measured {name} arm exceeded its {timeout:g}s bound"
                if arm.timed_out
                else f"the measured {name} arm {arm.unreadable_reason}"
                if arm.unreadable_reason
                else f"the measured {name} arm did not run"
            )
    if unreadable:
        return {"unreadable": unreadable}

    def left_out_of(arm_name: str) -> list[str]:
        return [name for name, arms in left_out.items() if arm_name in arms]

    # An empty base arm is the complete measurement of a scope whose tests the
    # change creates, and promotion reads it as the explicit ``none`` it
    # already accepts rather than as an arm with no log to prove it ran.
    baseline: Any = (
        arm_observation(
            _command_text(environment, base_argv),
            base_sha,
            base_arm,
            left_out=left_out_of("base"),
        )
        if base_tests or not scoped
        else "none"
    )
    return {
        "baseline_suite": baseline,
        "after_suite": arm_observation(
            _command_text(environment, head_argv),
            head_sha,
            head_arm,
            left_out=left_out_of("head"),
        ),
    }


def control_mutation_observation(
    *,
    command: str,
    tree: Path,
    base_tree: Path,
    base_sha: str,
    logs_dir: Path,
    test_files: Iterable[str] = (),
    run=None,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
    project: str = "",
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a node's own tests over the tree its repair is absent from.

    A node declares the mutation its check must fail against, and running that
    check over the unfixed base is the mutation performed: the node's tests are
    carried into a checkout of the run's base revision and run there, so a case
    that measures a restored guard fails exactly where the guard is missing.
    The failing ids are the control's own, judged by the same reader promotion
    uses for a log the run recorded.

    The tree the control runs over is the base revision, but a test file the
    node adds does not exist there. That file is carried from the run's own
    head, read from the delivered tree, which is the revision that holds it, so
    a control measure of an added test is a tree that can be built rather than
    an archive request for a path its revision does not carry.

    The run is submitted to the configured measurement partition the way an
    armed arm is, so taking the reading costs a queue wait rather than a suite
    run on the landing host. The return value carries ``unreadable`` when the
    command cannot be measured at all.
    """
    runner = run or _RUN
    node_tests = [str(path).strip() for path in test_files if str(path).strip()]
    environment, argv = armed_argv(command, node_tests)
    if not argv:
        return {"unreadable": "the run armed no suite command"}
    if _SCOPED_PLACEHOLDER in (command or "") and not node_tests:
        return {
            "unreadable": (
                "the armed command measures the node's own test files, "
                "and the node names none"
            )
        }
    if config is None:
        from reckon import flight

        config = flight.resolve(project or None, checkout_path=str(tree)).config
    landing = config.get("landing") or {}
    partition = str(landing.get("measure_partition") or "")
    if partition:
        runner = _partition_runner(
            runner,
            repo=tree,
            base_tree=base_tree,
            base_sha=base_sha,
            head_sha=_delivered_head(tree) or base_sha,
            partition=partition,
            long_partition=str(landing.get("measure_long_partition") or "rigel"),
            test_files=node_tests,
            config=config,
        )
        runner.partitioned = True
    _carry_missing_tests(base_tree, tree, node_tests)
    from reckon.crew.promotion_gate import _unused_log_path

    arm = _arm(
        argv,
        base_tree,
        _unused_log_path(Path(logs_dir) / "control-mutation.log"),
        runner,
        timeout,
        environment,
        file_count=len(node_tests),
    )
    if not arm.readable:
        return {
            "unreadable": (
                f"the control mutation exceeded its {timeout:g}s bound"
                if arm.timed_out
                else f"the control mutation {arm.unreadable_reason}"
                if arm.unreadable_reason
                else "the control mutation did not run"
            )
        }
    return {"control_suite": arm_observation(command, base_sha, arm)}


def _fails(
    command: Sequence[str],
    test_id: str,
    tree: Path,
    log_path: Path,
    runner,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
    file_count: int = 1,
) -> tuple[bool, str]:
    """Whether one isolated run reported *this* id as failing.

    Only the narrowed id counts: a failure the run reports for anything else
    is the pre-existing population the base arm already carries, and counting
    it here would make a regression read as failing equally at both revisions.

    The second value is the reason the arm could not be read, or "". An arm
    that did not run, or that exited non-zero without reporting any failure id
    — a usage error, a broken option — is not a passing run. Counting it as
    either passes or failures would silently answer the comparison, so the
    caller refuses the landing instead.
    """
    arm = _arm(
        command, tree, log_path, runner, timeout, environment, file_count=file_count
    )
    if not arm.readable:
        if arm.timed_out:
            return True, (
                f"the isolated arm for {test_id} exceeded its {timeout:g}s bound"
            )
        return True, f"the isolated arm for {test_id} did not run"
    if test_id in arm.failure_ids:
        return True, ""
    if arm.exit_status and not arm.failure_ids:
        return True, (
            f"the isolated arm for {test_id} exited {arm.exit_status} "
            "without reporting a failure id"
        )
    return False, ""


def _slug(test_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", test_id)


class BaseCheckoutError(ValueError):
    """The base checkout could not be built, so there is nothing to measure."""


def delivered_paths(repo: Path, base: str, head: str) -> list[str]:
    """Use the merge reader's strict diff and preserve its path boundaries."""
    from reckon.crew import merge

    try:
        return [path for _status, path in merge._changed_paths(repo, base, head)]
    except (OSError, merge.MergeError) as exc:
        raise BaseCheckoutError(
            f"the delivered changes could not be read: {exc}"
        ) from exc


@contextlib.contextmanager
def candidate_checkout(
    repo: Path, primary: str, head: str, timeout: float, *, tests_only: bool = False
) -> Iterator[tuple[str, Path]]:
    """Build the same merge as landing, and measure it without moving a branch."""
    from reckon.crew import merge

    try:
        candidate = merge.build_merge_commit(
            repo,
            primary,
            head,
            "merge: integrate the measured delivery",
            "Combine the reviewed delivery with the pinned primary revision "
            "for population measurement and landing.",
        )
    except (OSError, merge.MergeError) as exc:
        raise BaseCheckoutError(
            f"the merge candidate could not be built: {exc}"
        ) from exc
    with base_checkout(repo, candidate, timeout, tests_only=tests_only) as tree:
        yield candidate, tree


def _git(repo: Path, *arguments: str, timeout: float):
    """Run one bounded git command against ``repo``."""
    return _RUN(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def measures_on_partition(config: Mapping[str, Any] | None) -> bool:
    """Whether the project's landing measure submits its arms to a partition."""
    landing = (config or {}).get("landing") or {}
    return bool(str(landing.get("measure_partition") or "").strip())


#: The prefix every tree and reference directory a landing measure builds under
#: the node-local scratch root is named with.
BASE_TREE_PREFIX = "landing-base-"


def sweep_abandoned_base_trees(root: Path | None = None) -> list[dict[str, Any]]:
    """Remove a measure's trees that no live process holds, and report each one.

    A landing measure releases its own tree on every exit its process reaches.
    A tree whose process was killed — by a signal, or by the scheduler — never
    reaches that release, and the shared orphan sweep will not touch it until
    two hours of ctime grace have passed. This closes that gap: the next landing
    pass removes such a tree at once, because the fact that decides it is that
    the process which built the tree is gone, not how old the tree is.

    Only the trees this measure family builds are considered, and a tree a live
    process still holds is kept — a concurrent measure's tree is read from the
    process table the same way the orphan sweep reads it, so it is never
    removed. Every removal is printed to stderr with the tree's path and bytes
    so a reader can account for the space the pass reclaimed.
    """
    from reckon.crew.dispatch import tree_size_bytes

    scratch_root = worker_scratch_root() if root is None else Path(root)
    if not scratch_root.is_dir():
        return []
    candidates = sorted(
        child
        for child in scratch_root.iterdir()
        if child.name.startswith(BASE_TREE_PREFIX)
    )
    if not candidates:
        return []
    # Imported here rather than at module load: the sweep shares the process
    # table reader the orphan sweep uses, and gc reaches back into other crew
    # modules that import this one.
    from reckon.crew.gc import _scratch_process_holders

    holders = _scratch_process_holders(scratch_root)
    removed: list[dict[str, Any]] = []
    for child in candidates:
        if child.is_symlink() or not child.is_dir():
            continue
        if holders.get(child.name):
            continue
        try:
            size = tree_size_bytes(child)
        except OSError:
            size = 0
        shutil_rmtree(child)
        if child.exists():
            continue
        removed.append({"path": str(child), "bytes": size})
        print(
            f"landing-base sweep: removed {child} ({size} bytes)",
            file=sys.stderr,
            flush=True,
        )
    return removed


@contextlib.contextmanager
def base_tree_for_measure(
    repo: Path,
    base_sha: str,
    timeout: float,
    *,
    partitioned: bool,
    name: Path | None = None,
) -> Iterator[Path]:
    """The base tree a measurement's base arms run against.

    An arm submitted to a partition builds its own tree from the revision on
    the node that runs it, and the runner tells the base arm apart from the
    head arm by the directory it is given. So a partitioned measurement creates
    nothing on the fleet node: the directory it is handed is a name, never
    read, never created and never removed, which is what keeps a landing's arms
    from holding node-local disk while they wait for a job elsewhere. An
    in-place measurement runs in the tree, so it still gets the real checkout.
    """
    if not partitioned:
        with base_checkout(repo, base_sha, timeout) as tree:
            yield tree
        return
    # A partitioned measure builds nothing here, but a tree an earlier landing
    # whose process was killed left under the scratch root is reclaimed on this
    # pass: the fact that decides it is that its process is gone, not its age.
    root = worker_scratch_root()
    if root.is_dir():
        with contextlib.suppress(OSError):
            sweep_abandoned_base_trees(root)
    if name is not None:
        yield Path(name)
        return
    yield Path(tempfile.gettempdir()) / f"landing-base-ref-{base_sha}"


@contextlib.contextmanager
def base_checkout(
    repo: Path,
    base_sha: str,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
    *,
    tests_only: bool = False,
) -> Iterator[Path]:
    """A detached scratch tree at ``base_sha``, released when the block ends.

    The tree is built under ``worker_scratch_root()`` — the node-local root the
    fleet already owns, so a shared-storage ``TMPDIR`` cannot move the checkout
    onto GPFS — and it is released on every exit path, including a checkout that
    times out or fails before it is yielded. Every git call is bounded by the
    same ``timeout`` as the measurements.

    The release is the whole one every removal site makes: the tree is moved
    aside before git is told to forget it, so no deadline can leave a registered
    tree holding a fraction of its files. The scratch directory the tree was
    built in is the trash the move lands in, because it is on the tree's own
    filesystem — a node-local scratch tree cannot be renamed onto shared
    storage — and it is deleted whole afterwards.

    A release that fails leaves the repository's registration behind, so the
    registration is pruned before the block returns; a stale entry would
    otherwise make a later ``worktree add`` of the same path fail.

    A scratch root that cannot be created — full, or unwritable — raises
    ``BaseCheckoutError`` like any other checkout that cannot be built, so the
    caller refuses one run's landing rather than letting the error escape the
    admission pass.
    """
    from reckon.crew.routing import _release_worktree_whole

    try:
        root = worker_scratch_root()
        root.mkdir(parents=True, exist_ok=True)
        sweep_abandoned_base_trees(root)
        scratch = Path(tempfile.mkdtemp(prefix="landing-base-", dir=root))
    except OSError as exc:
        # A full or unwritable scratch root is a checkout that cannot be built,
        # not a reason to stop the pass: the caller refuses this run's landing.
        raise BaseCheckoutError(
            f"could not create a scratch tree for {base_sha}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    # An open descriptor protects a measurement even while its parent waits
    # outside the checkout: the scratch sweep sees this holder through /proc.
    with (scratch / ".measurement-owner").open("w"):
        target = scratch / "tree"
        try:
            added = _git(
                repo,
                "worktree",
                "add",
                "--detach",
                *(("--no-checkout",) if tests_only else ()),
                str(target),
                base_sha,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as expired:
            shutil_rmtree(scratch)
            raise BaseCheckoutError(
                f"could not check out {base_sha} for the base arm: "
                f"git worktree add exceeded its {timeout:g}s bound"
            ) from expired
        except OSError as exc:
            shutil_rmtree(scratch)
            raise BaseCheckoutError(
                f"could not check out {base_sha} for the base arm: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if added.returncode:
            shutil_rmtree(scratch)
            raise BaseCheckoutError(
                f"could not check out {base_sha} for the base arm: "
                f"{(added.stderr or added.stdout or '').strip()}"
            )
        if tests_only:
            try:
                _populate_tests_and_top_level(repo, target, base_sha, timeout)
            except BaseCheckoutError:
                with contextlib.suppress(Exception):
                    _release_worktree_whole(
                        repo, target, run_id="landing-base", trash_root=scratch
                    )
                shutil_rmtree(scratch)
                raise
        _link_environment(repo, target)
        try:
            yield target
        finally:
            released = False
            with contextlib.suppress(Exception):
                released = bool(
                    _release_worktree_whole(
                        repo, target, run_id="landing-base", trash_root=scratch
                    ).get("released")
                )
            shutil_rmtree(scratch)
            if not released:
                # The tree is gone from disk but the repository still registers it.
                # Prune drops that stale entry, and ``--expire now`` is what makes
                # it act immediately rather than after the default grace period.
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    _git(repo, "worktree", "prune", "--expire", "now", timeout=timeout)


def _populate_tests_and_top_level(
    repo: Path, target: Path, revision: str, timeout: float
) -> None:
    """Write a revision's ``tests`` directory and top-level files into ``target``.

    A partitioned measurement reads only these from its local tree: the test
    files it selects and scans for imports, and the project files at the top.
    The arms build their own full trees on the nodes that run them, so the rest
    of a checkout would be gigabytes on the fleet node that nothing reads.
    """
    listed = _git(repo, "ls-tree", revision, timeout=timeout)
    if listed.returncode:
        raise BaseCheckoutError(
            f"could not list {revision}: {(listed.stderr or '').strip()}"
        )
    names = []
    for line in listed.stdout.splitlines():
        meta, _, name = line.partition("\t")
        kind = meta.split()[1] if len(meta.split()) > 1 else ""
        if kind == "blob" or (kind == "tree" and name == "tests"):
            names.append(name)
    if not names:
        return
    archived = _RUN(
        ["git", "-C", str(repo), "archive", revision, "--", *names],
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if archived.returncode:
        raise BaseCheckoutError(
            f"could not read the tests of {revision}: "
            f"{bytes(archived.stderr or b'').decode(errors='replace').strip()}"
        )
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
        archive.extractall(target, filter="data")


def shutil_rmtree(path: Path) -> None:
    """Remove a scratch tree, ignoring a removal that already happened."""
    shutil.rmtree(path, ignore_errors=True)
