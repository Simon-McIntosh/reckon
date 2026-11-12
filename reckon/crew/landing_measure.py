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
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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

#: The token an armed command carries in place of the node's own test files.
_SCOPED_PLACEHOLDER = "{node_test_files}"

#: A leading ``NAME=value`` token, which is the arm's environment rather than
#: the program it starts.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_RUN = subprocess.run


@dataclass(frozen=True)
class Arm:
    """One command run at one revision, with what it reported."""

    command: tuple[str, ...]
    exit_status: int | None
    failure_ids: tuple[str, ...]
    log_path: str = ""
    timed_out: bool = False
    #: Wall-clock seconds the arm took, and how many files its population
    #: read. The pair is what a later arm's bound is sized from, so an arm
    #: that took 900 s is not judged again under a bound that ignores it.
    elapsed_seconds: float = 0.0
    file_count: int = 0

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
            "elapsed_seconds": self.elapsed_seconds,
            "file_count": self.file_count,
        }


@dataclass(frozen=True)
class Measurement:
    """What the three readings found for one delivered run."""

    command: str
    suite: dict[str, Any]
    population: dict[str, Any]
    isolated: dict[str, dict[str, list[bool]]] = field(default_factory=dict)
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
    environment = repo / ".venv"
    if not environment.exists():
        return
    link = tree / ".venv"
    if link.exists() or link.is_symlink():
        return
    with contextlib.suppress(OSError):
        link.symlink_to(environment.resolve())


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
        mode = "a" if getattr(run, "partitioned", False) else "w"
        with log_path.open(mode, encoding="utf-8") as stream:
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
        log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        return Arm(
            tuple(argv),
            None,
            (),
            str(log_path),
            elapsed_seconds=elapsed,
            file_count=file_count,
        )
    elapsed = time.monotonic() - started
    output = f"{finished.stdout or ''}{finished.stderr or ''}"
    log_path.write_text(output, encoding="utf-8")
    return Arm(
        tuple(argv),
        int(finished.returncode),
        failing_ids(output),
        str(log_path),
        elapsed_seconds=elapsed,
        file_count=file_count,
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
    environment = (repo / ".venv").resolve()
    carried = tuple(test_files)

    def run(command, **options):
        revision = base_sha if Path(options["cwd"]) == base_tree else head_sha
        timeout = options["timeout"]
        selected = long_partition if timeout > 3600 else partition
        seconds = max(1, math.ceil(timeout))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        bound = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        log = Path(options.pop("arm_log_path")).resolve()
        scratch_id = hashlib.sha256(str(log).encode()).hexdigest()[:12]
        quote = shlex.quote
        git = shlex.join(["git", "--git-dir", str(git_dir)])
        carry = "\n".join(
            f'if [ ! -e "$scratch/tree"/{quote(relative)} ]; then '
            f"{git} archive {quote(head_sha)} -- {quote(relative)} | "
            'tar -x -C "$scratch/tree"; fi'
            for relative in carried
        )
        assignments = options.pop("arm_environment")
        invocation = shlex.join(
            ["env", *[f"{k}={v}" for k, v in assignments.items()], *command]
        )
        script = f"""set -euo pipefail
scratch=
trap 'status=$?; if [ -n "$scratch" ]; then rm -rvf "$scratch"; fi; printf "EXIT=%s\n" "$status"' EXIT
export TMPDIR={NODE_LOCAL_TEMP_ROOT}
scratch=$(mktemp -d "{NODE_LOCAL_TEMP_ROOT}/$USER-landing-{scratch_id}-XXXXXX")
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
        # The batch job owns the step: srun from the fleet itself would stay
        # inside the fleet allocation even when it names a different partition.
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
            + " > "
            + quote(str(log))
            + " 2>&1"
        )
        argv = [
            "sbatch",
            "--wait",
            "--parsable",
            f"--partition={selected}",
            f"--time={bound}",
            "--nodes=1",
            "--ntasks=1",
            f"--cpus-per-task={POPULATION_WORKERS}",
            "--mem=32G",
            f"--output={log}.scheduler",
            f"--error={log}.scheduler",
            f"--chdir={shared_repo}",
            "--wrap",
            step,
        ]
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SLURM_", "SBATCH_"))
        }
        env["RECKON_ALLOW_NESTED_LAUNCH"] = "1"
        env["TMPDIR"] = NODE_LOCAL_TEMP_ROOT
        log.write_text("")
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
        # Only the arm receipt can report a test exit. A failed submission or a
        # job killed before its receipt is unreadable even if stdout looks green.
        receipt = re.search(r"^EXIT=(\d+)\n?\Z", output, re.MULTILINE)
        status = int(receipt.group(1)) if receipt else 3
        if status != finished.returncode:
            status = 3
        return subprocess.CompletedProcess(
            command,
            status,
            output or f"{finished.stdout or ''}{finished.stderr or ''}",
            "",
        )

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
    selecting importers of the delivered change.
    """
    runner = run or _RUN
    if config is None:
        from reckon import flight

        config = flight.resolve(project or None, checkout_path=str(tree)).config
    landing = config.get("landing") or {}
    partition = str(landing.get("measure_partition") or "")
    execution = "slurm" if partition else "in-place"
    node_tests = [str(path).strip() for path in test_files if str(path).strip()]
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
    files = importer_population(
        changed_paths(tree, base_sha, head_sha) if changed is None else changed, tree
    )
    if partition:
        runner = _partition_runner(
            runner,
            repo=tree,
            base_tree=base_tree,
            base_sha=base_sha,
            head_sha=head_sha,
            partition=partition,
            long_partition=str(landing.get("measure_long_partition") or "rigel"),
            test_files=[*node_tests, *files],
        )
        runner.partitioned = True
    _carry_missing_tests(base_tree, tree, node_tests)
    logs = Path(logs_dir)
    suite_bound = bound(len(node_tests))
    suite_base = _arm(
        argv,
        base_tree,
        logs / "suite-base.log",
        runner,
        suite_bound,
        environment,
        file_count=len(node_tests),
    )
    suite_head = _arm(
        argv,
        tree,
        logs / "suite-head.log",
        runner,
        suite_bound,
        environment,
        file_count=len(node_tests),
    )
    _carry_missing_tests(base_tree, tree, files)
    population_command = [*argv, *files, "-n", str(POPULATION_WORKERS)]
    population_bound = bound(len(files))
    if files:
        population_base = _arm(
            population_command,
            base_tree,
            logs / "population-base.log",
            runner,
            population_bound,
            environment,
            file_count=len(files),
        )
        population_head = _arm(
            population_command,
            tree,
            logs / "population-head.log",
            runner,
            population_bound,
            environment,
            file_count=len(files),
        )
    else:
        population_base = Arm(tuple(population_command), None, (), "")
        population_head = Arm(tuple(population_command), None, (), "")
    suite = {
        "command": command,
        "base": suite_base.as_dict(),
        "head": suite_head.as_dict(),
    }
    population = {
        "files": list(files),
        "base": population_base.as_dict(),
        "head": population_head.as_dict(),
    }
    unreadable = ""
    for name, arm, arm_bound_seconds in (
        ("base", suite_base, suite_bound),
        ("head", suite_head, suite_bound),
        *(
            (
                ("population base", population_base, population_bound),
                ("population head", population_head, population_bound),
            )
            if files
            else ()
        ),
    ):
        if not arm.readable:
            unreadable = (
                f"the {name} arm exceeded its {arm_bound_seconds:g}s bound"
                if arm.timed_out
                else f"the {name} arm did not run"
            )
            break
    added_base = {*suite_base.failure_ids, *population_base.failure_ids}
    added_head = [*suite_head.failure_ids, *population_head.failure_ids]
    uncovered = failure_ids_covered(added_base, added_head)
    added = list(
        dict.fromkeys(test_id for test_id in added_head if test_id in uncovered)
    )
    isolated: dict[str, dict[str, list[bool]]] = {}

    def narrowed(test_id: str, arm_tree: Path, arm: str, n: int) -> tuple[bool, str]:
        return _fails(
            isolated_command(argv, test_id, node_tests),
            test_id,
            arm_tree,
            logs / "isolated" / f"{_slug(test_id)}-{arm}-{n}.log",
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
    command: str, revision: str, arm: Arm, *, measured_by: str = "landing"
) -> dict[str, Any]:
    """One arm as the suite-observation record a run's manifest carries.

    The shape is the one promotion already reads for ``baseline_suite`` and
    ``after_suite``, so an arm the landing measured is judged by the same code
    and the same refusal as an arm the run recorded.
    """
    return {
        "revision": revision,
        "command": command,
        "exit_status": arm.exit_status,
        "completed": arm.exit_status in (0, 1),
        "failure_count": len(arm.failure_ids),
        "failure_ids": list(arm.failure_ids),
        "log_path": arm.log_path,
        "measured_by": measured_by,
    }


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
    partition = str(landing.get("measure_partition") or "")
    if partition:
        runner = _partition_runner(
            runner,
            repo=tree,
            base_tree=base_tree,
            base_sha=base_sha,
            head_sha=head_sha,
            partition=partition,
            long_partition=str(landing.get("measure_long_partition") or "rigel"),
            test_files=node_tests,
        )
        runner.partitioned = True
    _carry_missing_tests(base_tree, tree, node_tests)
    logs = Path(logs_dir)
    base_arm = _arm(
        argv,
        base_tree,
        logs / "armed-baseline.log",
        runner,
        timeout,
        environment,
        file_count=len(node_tests),
    )
    head_arm = _arm(
        argv,
        tree,
        logs / "armed-after.log",
        runner,
        timeout,
        environment,
        file_count=len(node_tests),
    )
    unreadable = ""
    for name, arm in (("baseline", base_arm), ("after", head_arm)):
        if not arm.readable and not unreadable:
            unreadable = (
                f"the measured {name} arm exceeded its {timeout:g}s bound"
                if arm.timed_out
                else f"the measured {name} arm did not run"
            )
    if unreadable:
        return {"unreadable": unreadable}
    return {
        "baseline_suite": arm_observation(command, base_sha, base_arm),
        "after_suite": arm_observation(command, head_sha, head_arm),
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
            head_sha=base_sha,
            partition=partition,
            long_partition=str(landing.get("measure_long_partition") or "rigel"),
            test_files=node_tests,
        )
        runner.partitioned = True
    _carry_missing_tests(base_tree, tree, node_tests)
    arm = _arm(
        argv,
        base_tree,
        Path(logs_dir) / "control-mutation.log",
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
    repo: Path, primary: str, head: str, timeout: float
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
    with base_checkout(repo, candidate, timeout) as tree:
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


@contextlib.contextmanager
def base_checkout(
    repo: Path, base_sha: str, timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS
) -> Iterator[Path]:
    """A detached scratch tree at ``base_sha``, removed when the block ends.

    The tree is built under ``worker_scratch_root()`` — the node-local root the
    fleet already owns, so a shared-storage ``TMPDIR`` cannot move the checkout
    onto GPFS — and it is removed on every exit path, including a checkout that
    times out or fails before it is yielded. Every git call is bounded by the
    same ``timeout`` as the measurements.

    A removal that fails or times out leaves the repository's registration
    behind, so the registration is pruned before the block returns; a stale
    entry would otherwise make a later ``worktree add`` of the same path fail.

    A scratch root that cannot be created — full, or unwritable — raises
    ``BaseCheckoutError`` like any other checkout that cannot be built, so the
    caller refuses one run's landing rather than letting the error escape the
    admission pass.
    """
    try:
        root = worker_scratch_root()
        root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="landing-base-", dir=root))
    except OSError as exc:
        # A full or unwritable scratch root is a checkout that cannot be built,
        # not a reason to stop the pass: the caller refuses this run's landing.
        raise BaseCheckoutError(
            f"could not create a scratch tree for {base_sha}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    target = scratch / "tree"
    try:
        added = _git(
            repo, "worktree", "add", "--detach", str(target), base_sha, timeout=timeout
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
    _link_environment(repo, target)
    try:
        yield target
    finally:
        removed = False
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            finished = _git(
                repo, "worktree", "remove", "--force", str(target), timeout=timeout
            )
            removed = finished.returncode == 0
        shutil_rmtree(scratch)
        if not removed:
            # The tree is gone from disk but the repository still registers it.
            # Prune drops that stale entry, and ``--expire now`` is what makes
            # it act immediately rather than after the default grace period.
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _git(repo, "worktree", "prune", "--expire", "now", timeout=timeout)


def shutil_rmtree(path: Path) -> None:
    """Remove a scratch tree, ignoring a removal that already happened."""
    shutil.rmtree(path, ignore_errors=True)
