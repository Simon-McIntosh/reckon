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

Every subprocess the measurement starts — the suite arms, the isolated re-runs
and the base checkout's git worktree calls — carries a wall-clock bound. A run
that exceeds it is unmeasurable rather than failing, so the landing is refused
and nothing is read from a partial log.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reckon.clones import changed_paths

#: How many times an added id is re-run alone at each revision.
REPEATS = 3

#: Parallelism for the importer population.
POPULATION_WORKERS = 8

#: Wall-clock bound for one measurement subprocess, in seconds. A flight config
#: may retune it under ``landing.measure_timeout_seconds``; this is the bound
#: used when it declares none, and it is deliberately modest because the
#: landing pass runs inside the admission loop.
DEFAULT_MEASURE_TIMEOUT_SECONDS = 600.0

#: A line a failing test runner reports its failing id on.
_FAILED_ID = re.compile(r"^(?:FAILED|ERROR)[ \t]+(\S+)", re.MULTILINE)

_RUN = subprocess.run


@dataclass(frozen=True)
class Arm:
    """One command run at one revision, with what it reported."""

    command: tuple[str, ...]
    exit_status: int | None
    failure_ids: tuple[str, ...]
    log_path: str = ""
    timed_out: bool = False

    @property
    def readable(self) -> bool:
        return self.exit_status is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_status": self.exit_status,
            "failure_ids": list(self.failure_ids),
            "log_path": self.log_path,
            "timed_out": self.timed_out,
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "head": self.head,
            "suite": self.suite,
            "population": self.population,
            "isolated": self.isolated,
            "real": list(self.real),
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
    for path in sorted(tests_root.rglob("test_*.py")):
        text = ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(_imports_module(text, module) for module in modules):
            population.append(path.relative_to(tree).as_posix())
    return tuple(population)


def _imports_module(text: str, module: str) -> bool:
    """Whether a file's text imports ``module``, by dotted name or by leaf."""
    if module in text:
        return True
    leaf = module.rsplit(".", 1)[-1]
    return any(
        pattern in text
        for pattern in (
            f"import {leaf}\n",
            f"import {leaf},",
            f"import {leaf} ",
            f"from {module}",
        )
    )


def _target_name(token: str) -> str:
    """A command token with the path spelling that names the same file removed."""
    name = str(token).strip()
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def runner_argv(command: Sequence[str], test_files: Iterable[str] = ()) -> list[str]:
    """The armed command with the node's own test files removed.

    The armed command is what the run's promotion runs, so a single id is
    re-run through the same interpreter, environment, wrappers, plugins and
    options. Which tokens are its test targets is not guessed from their
    shape: they are exactly the node's test files the command was resolved
    from, so every other token — an environment assignment, a ``timeout`` or
    ``env`` prefix, an option and its value — is kept and only the files are
    dropped.
    """
    targets = {_target_name(path) for path in test_files if str(path).strip()}
    return [token for token in command if _target_name(token) not in targets]


def isolated_command(
    command: Sequence[str], test_id: str, test_files: Iterable[str] = ()
) -> list[str]:
    """The armed command narrowed to exactly one test id."""
    return [*runner_argv(command, test_files), test_id]


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


def scratch_root() -> Path:
    """Where the base checkout is built: node-local temporary storage.

    The base checkout is a throwaway tree for one measurement, so it belongs on
    local disk rather than on the repository's shared filesystem, where a copy
    of the tree costs every peer client.
    """
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name, "").strip()
        if value and Path(value).is_dir():
            return Path(value)
    return Path("/tmp")  # noqa: S108 - the documented node-local fallback


def _arm(
    command: Sequence[str],
    tree: Path,
    log_path: Path,
    run,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
) -> Arm:
    """Run one command at one tree and record its failure reported ids.

    The command is bounded: a run that exceeds ``timeout`` is recorded as
    unreadable and no failure id is taken from whatever it had printed, because
    a partial log does not say whether the id after it would have failed.
    """
    argv = list(command)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        finished = run(
            argv,
            cwd=str(tree),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        partial = f"{expired.stdout or ''}{expired.stderr or ''}"
        log_path.write_text(f"timed out after {timeout}s\n{partial}", encoding="utf-8")
        return Arm(tuple(argv), None, (), str(log_path), timed_out=True)
    except OSError as exc:
        log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        return Arm(tuple(argv), None, (), str(log_path))
    output = f"{finished.stdout or ''}{finished.stderr or ''}"
    log_path.write_text(output, encoding="utf-8")
    return Arm(
        tuple(argv),
        int(finished.returncode),
        failing_ids(output),
        str(log_path),
    )


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
) -> Measurement:
    """Run the three readings for one delivered run and return the verdict.

    ``test_files`` are the node's own test files, the ones the armed command
    was resolved from; they are what a caller's single-id re-run replaces.
    ``timeout`` bounds every subprocess the measurement starts. ``run`` is the
    command runner, ``subprocess.run``-shaped, so a caller can drive the
    measurement without executing a real suite.
    """
    runner = run or _RUN
    argv = shlex.split(command or "")
    if not argv:
        return Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the run armed no suite command",
            head=head_sha,
        )
    logs = Path(logs_dir)
    suite_base = _arm(argv, base_tree, logs / "suite-base.log", runner, timeout)
    suite_head = _arm(argv, tree, logs / "suite-head.log", runner, timeout)
    files = importer_population(changed_paths(tree, base_sha, head_sha), tree)
    population_command = [*argv, *files, "-n", str(POPULATION_WORKERS)]
    if files:
        population_base = _arm(
            population_command, base_tree, logs / "population-base.log", runner, timeout
        )
        population_head = _arm(
            population_command, tree, logs / "population-head.log", runner, timeout
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
    for name, arm in (
        ("base", suite_base),
        ("head", suite_head),
        *(
            (("population base", population_base), ("population head", population_head))
            if files
            else ()
        ),
    ):
        if not arm.readable:
            unreadable = (
                f"the {name} arm exceeded its {timeout:g}s bound"
                if arm.timed_out
                else f"the {name} arm did not run"
            )
            break
    added_base = {*suite_base.failure_ids, *population_base.failure_ids}
    added_head = [*suite_head.failure_ids, *population_head.failure_ids]
    added: list[str] = []
    for test_id in added_head:
        if test_id not in added_base and test_id not in added:
            added.append(test_id)
    isolated: dict[str, dict[str, list[bool]]] = {}

    def narrowed(test_id: str, arm_tree: Path, arm: str, n: int) -> tuple[bool, str]:
        return _fails(
            isolated_command(argv, test_id, test_files),
            test_id,
            arm_tree,
            logs / "isolated" / f"{_slug(test_id)}-{arm}-{n}.log",
            runner,
            timeout,
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
        real=real,
        unreadable=unreadable,
        head=head_sha,
    )


def _fails(
    command: Sequence[str],
    test_id: str,
    tree: Path,
    log_path: Path,
    runner,
    timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS,
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
    arm = _arm(command, tree, log_path, runner, timeout)
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


@contextlib.contextmanager
def base_checkout(
    repo: Path, base_sha: str, timeout: float = DEFAULT_MEASURE_TIMEOUT_SECONDS
) -> Iterator[Path]:
    """A detached scratch tree at ``base_sha``, removed when the block ends.

    The tree is built under ``scratch_root()`` — node-local temporary storage,
    never the repository's shared filesystem — and it is removed on every exit
    path, including a checkout that times out or fails before it is yielded.
    Both git calls are bounded by the same ``timeout`` as the measurements.
    """
    scratch = Path(tempfile.mkdtemp(prefix="landing-base-", dir=scratch_root()))
    target = scratch / "tree"
    try:
        added = _RUN(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                str(target),
                base_sha,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        shutil_rmtree(scratch)
        raise ValueError(
            f"could not check out {base_sha} for the base arm: "
            f"git worktree add exceeded its {timeout:g}s bound"
        ) from expired
    except OSError as exc:
        shutil_rmtree(scratch)
        raise ValueError(
            f"could not check out {base_sha} for the base arm: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if added.returncode:
        shutil_rmtree(scratch)
        raise ValueError(
            f"could not check out {base_sha} for the base arm: "
            f"{(added.stderr or added.stdout or '').strip()}"
        )
    try:
        yield target
    finally:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            _RUN(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        shutil_rmtree(scratch)


def shutil_rmtree(path: Path) -> None:
    """Remove a scratch tree, ignoring a removal that already happened."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)
