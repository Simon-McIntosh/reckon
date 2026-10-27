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
"""

from __future__ import annotations

import contextlib
import re
import shlex
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reckon.clones import changed_paths

#: How many times an added id is re-run alone at each revision.
REPEATS = 3

#: Parallelism for the importer population.
POPULATION_WORKERS = 8

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

    @property
    def readable(self) -> bool:
        return self.exit_status is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_status": self.exit_status,
            "failure_ids": list(self.failure_ids),
            "log_path": self.log_path,
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "suite": self.suite,
            "population": self.population,
            "isolated": self.isolated,
            "real": list(self.real),
            "unreadable": self.unreadable,
        }


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


#: What a token that names a test target looks like, as opposed to an option
#: or an option's value: a path separator, a ``.py`` suffix or a node id.
_TARGET_HINT = re.compile(r"/|\.py$|::")


def runner_argv(command: Sequence[str]) -> list[str]:
    """The armed command with its test targets removed, keeping its options.

    The armed command is what the run's promotion runs, so a single id is
    re-run through the same interpreter, plugins and options. Its own test
    targets are dropped: leaving them in would run the whole file again and
    any failure already present there would be counted against the id.
    """
    kept: list[str] = []
    for token in command:
        if kept and not token.startswith("-") and _TARGET_HINT.search(token):
            continue
        kept.append(token)
    return kept


def isolated_command(command: Sequence[str], test_id: str) -> list[str]:
    """The armed command narrowed to exactly one test id."""
    return [*runner_argv(command), test_id]


def _arm(
    command: Sequence[str],
    tree: Path,
    log_path: Path,
    run,
) -> Arm:
    """Run one command at one tree and record its failure reported ids."""
    argv = list(command)
    try:
        finished = run(
            argv,
            cwd=str(tree),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        return Arm(tuple(argv), None, (), str(log_path))
    output = f"{finished.stdout or ''}{finished.stderr or ''}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
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
    run=None,
    repeats: int = REPEATS,
) -> Measurement:
    """Run the three readings for one delivered run and return the verdict.

    ``run`` is the command runner, ``subprocess.run``-shaped, so a caller can
    drive the measurement without executing a real suite.
    """
    runner = run or _RUN
    argv = shlex.split(command or "")
    if not argv:
        return Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the run armed no suite command",
        )
    logs = Path(logs_dir)
    suite_base = _arm(argv, base_tree, logs / "suite-base.log", runner)
    suite_head = _arm(argv, tree, logs / "suite-head.log", runner)
    files = importer_population(changed_paths(tree, base_sha, head_sha), tree)
    population_command = [*argv, *files, "-n", str(POPULATION_WORKERS)]
    if files:
        population_base = _arm(
            population_command, base_tree, logs / "population-base.log", runner
        )
        population_head = _arm(
            population_command, tree, logs / "population-head.log", runner
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
            unreadable = f"the {name} arm did not run"
            break
    added_base = {*suite_base.failure_ids, *population_base.failure_ids}
    added_head = [*suite_head.failure_ids, *population_head.failure_ids]
    added: list[str] = []
    for test_id in added_head:
        if test_id not in added_base and test_id not in added:
            added.append(test_id)
    isolated: dict[str, dict[str, list[bool]]] = {}
    for test_id in added:
        meta = isolated_command(argv, test_id)
        isolated[test_id] = {
            "base": [
                _fails(
                    meta,
                    test_id,
                    base_tree,
                    logs / "isolated" / f"{_slug(test_id)}-base-{n}.log",
                    runner,
                )
                for n in range(1, repeats + 1)
            ],
            "head": [
                _fails(
                    meta,
                    test_id,
                    tree,
                    logs / "isolated" / f"{_slug(test_id)}-head-{n}.log",
                    runner,
                )
                for n in range(1, repeats + 1)
            ],
        }
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
    )


def _fails(
    command: Sequence[str], test_id: str, tree: Path, log_path: Path, runner
) -> bool:
    """Whether one isolated run reported *this* id as failing.

    Only the narrowed id counts: a failure the run reports for anything else
    is the pre-existing population the base arm already carries, and counting
    it here would make a regression read as failing equally at both revisions.
    A run that could not be read is counted as a failure, so an unmeasurable
    arm refuses the landing rather than clearing it.
    """
    arm = _arm(command, tree, log_path, runner)
    if not arm.readable:
        return True
    return test_id in arm.failure_ids


def _slug(test_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", test_id)


@contextlib.contextmanager
def base_checkout(repo: Path, base_sha: str) -> Iterator[Path]:
    """A detached scratch tree at ``base_sha``, removed when the block ends."""
    scratch = Path(tempfile.mkdtemp(prefix="landing-base-"))
    target = scratch / "tree"
    added = _RUN(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(target), base_sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        raise ValueError(
            f"could not check out {base_sha} for the base arm: "
            f"{(added.stderr or added.stdout or '').strip()}"
        )
    try:
        yield target
    finally:
        _RUN(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil_rmtree(scratch)


def shutil_rmtree(path: Path) -> None:
    """Remove a scratch tree, ignoring a removal that already happened."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)
