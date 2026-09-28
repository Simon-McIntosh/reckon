"""Run a project's declared suite once, record what it observed, and hold on a bad one.

A per-node gate runs only the tests a node's brief names, so a project whose
default command stopped at collection can keep promoting unseen. This module is
the project-wide check that sees the whole tree: it runs the command a project
declares in ``review.suite``, bounded by the whole-run budget the same block
names, and writes one JSON record per run under the project's state directory.

:func:`run` executes the declared command from the checkout root, kills the
whole process group at the budget, and returns a record of what the log shows.
:func:`record` writes that record where a later reader can find it.
:func:`hold_reason` answers why a tier ``none`` or ``light`` promotion must
wait: the latest recorded run failed to collect or overran its budget, and no
later waiver has excused it. :func:`record_waiver` writes that waiver, naming
who excused it and why.

The FAILED and ERROR ids are read with the parser in
:mod:`reckon.crew.review`, and the result-count tokens with the parser in
:mod:`reckon.crew.promotion`, so a log is read the one way this repository
already reads a gate log.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._store import state_path
from reckon.crew.promotion import _RUNNER_SUMMARY
from reckon.crew.review import _pytest_failure_ids
from reckon.flight import SuiteDeclaration

# The two tiers the standing suite holds back. A full review reads the run for
# itself, so it is not made to wait on the project's suite; a lighter review is
# only worth its cost beside a check that sees the whole project.
HELD_TIERS = frozenset({"none", "light"})

# The state subdirectory, under a project's docs state tree, that holds one
# file per suite run and one file per waiver.
SUITE_RUNS_DIRNAME = "suite-runs"

# The kind a record carries, so a reader can tell a run from a waiver without
# reading the file name alone.
RUN_KIND = "run"
WAIVER_KIND = "waiver"

# The plain pytest collection line ("collected 12 items") and the pytest-xdist
# form (`[12 items]`), each carrying the count the runner collected.
_COLLECTED_RE = re.compile(r"collected\s+(\d+)\s+items?")
_XDIST_ITEMS_RE = re.compile(r"\[\s*(\d+)\s+items?\s*\]")

# One result-count token ("518 passed") split into its number and its word, so
# a token the runner-summary parser matched can be attributed to its category.
_RUNNER_TOKEN_RE = re.compile(r"(\d+)\s+(\w+)")

# The runner words that name each count this record carries.
_FAILED_WORDS = {"failed"}
_ERRORED_WORDS = {"error", "errors"}


def suite_runs_dir(project_root: str | Path, project: str) -> Path:
    """Return the directory holding one file per suite run for ``project``.

    Resolved through the same project-state resolver the rest of reckon uses,
    so a checkout's state tree is named one way: ``state_path`` gives the
    project's state directory, and the suite-runs directory sits beside it.
    """
    return (
        state_path(project, SUITE_RUNS_DIRNAME, root=project_root).parent
        / SUITE_RUNS_DIRNAME
    )


def _observed_at() -> str:
    """The observation stamp, at microsecond resolution.

    A run and a waiver can be written inside the same second, and the hold
    reads which is later; a second-resolution stamp would tie them and leave
    the hold reading whichever the directory listing happened to sort first.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _revision(project_root: str | Path) -> str:
    """Return the revision the suite ran at, read from the checkout's own HEAD."""
    result = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill every process the suite spawned, not only the runner itself.

    A test that forks leaves children behind the runner's own exit; signalling
    the process group is what stops them. The runner is started in its own
    session for exactly this, so its group id is its own pid.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _collected(log_text: str) -> int | None:
    """The number of items the runner collected, from either output form."""
    match = _COLLECTED_RE.search(log_text)
    if match is not None:
        return int(match.group(1))
    match = _XDIST_ITEMS_RE.search(log_text)
    if match is not None:
        return int(match.group(1))
    return None


def _result_counts(log_text: str) -> dict[str, int | None]:
    """The passed, failed and errored counts a runner summary reports.

    The tokens are matched by the runner-summary parser this repository already
    uses for a gate log; each matched token is then split into its number and
    its word and attributed to a count. The last occurrence of a word wins, so
    a log that quotes an earlier summary does not shadow the final one.
    """
    counts: dict[str, int | None] = {"passed": None, "failed": None, "errored": None}
    for token in _RUNNER_SUMMARY.findall(log_text):
        match = _RUNNER_TOKEN_RE.fullmatch(token.strip())
        if match is None:
            continue
        number, word = int(match.group(1)), match.group(2).lower()
        if word == "passed":
            counts["passed"] = number
        elif word in _FAILED_WORDS:
            counts["failed"] = number
        elif word in _ERRORED_WORDS:
            counts["errored"] = number
    return counts


def run(
    project_root: str | Path,
    declaration: SuiteDeclaration,
    log_path: str | Path,
) -> dict[str, Any]:
    """Run ``declaration`` from ``project_root`` and record what its log shows.

    The command runs in its own session so the whole group can be stopped at
    the budget; the combined output is captured to ``log_path``. The returned
    record carries the revision the run was taken at, the command, the exit
    status, the collected/passed/failed/errored counts, the duration and
    budget, and the two failure flags a hold reads.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    revision = _revision(project_root)
    budget_seconds = declaration.budget_seconds()
    command = list(declaration.command)

    started = time.monotonic()
    over_budget = False
    exit_status: int | None = None
    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(project_root),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log_file.write(f"could not start {command!r}: {exc}\n")
            log_file.write("EXIT=127\n")
            exit_status = 127
        else:
            try:
                exit_status = proc.wait(timeout=budget_seconds)
            except subprocess.TimeoutExpired:
                over_budget = True
                _kill_process_group(proc)
                exit_status = proc.wait()
            log_file.write(f"EXIT={exit_status if exit_status is not None else 127}\n")

    text = log_path.read_text(encoding="utf-8", errors="replace")
    return _build_record(
        revision=revision,
        command=command,
        exit_status=exit_status,
        log_text=text,
        log_path=log_path,
        duration_seconds=time.monotonic() - started,
        budget_seconds=budget_seconds,
        over_budget=over_budget,
    )


def _build_record(
    *,
    revision: str,
    command: list[str],
    exit_status: int | None,
    log_text: str,
    log_path: Path,
    duration_seconds: float,
    budget_seconds: int,
    over_budget: bool,
) -> dict[str, Any]:
    """Assemble a run record from the parts the caller measured and the log."""
    counts = _build_counts(log_text, exit_status)
    return {
        "kind": RUN_KIND,
        "revision": revision,
        "command": command,
        "exit_status": exit_status,
        "collected": counts["collected"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errored": counts["errored"],
        "duration_seconds": round(duration_seconds, 3),
        "budget_seconds": budget_seconds,
        "collection_failed": counts["collection_failed"],
        "over_budget": over_budget,
        "failure_ids": counts["failure_ids"],
        "log_path": str(log_path),
        "observed_at": _observed_at(),
    }


def _build_counts(log_text: str, exit_status: int | None) -> dict[str, Any]:
    """The counts and flags one log and exit status support.

    A run failed to collect when it exited non-zero having collected nothing:
    the runner stopped before it ran a test. A run that collected items and
    then failed is an ordinary failing suite, not a collection failure.
    """
    collected = _collected(log_text)
    results = _result_counts(log_text)
    failure_ids = sorted(_pytest_failure_ids(log_text))
    collection_failed = exit_status not in (None, 0) and (collected in (None, 0))
    return {
        "collected": collected,
        "passed": results["passed"],
        "failed": results["failed"],
        "errored": results["errored"],
        "collection_failed": collection_failed,
        "failure_ids": failure_ids,
    }


def record(
    project_root: str | Path,
    project: str,
    run_record: Mapping[str, Any],
) -> Path:
    """Write one run record under the project's suite-runs directory.

    The file is named by the observation time, so a directory listing sorts by
    when each run was taken. The write is atomic through the writer this
    repository already uses for a JSON record, so a reader never sees a
    half-written file.
    """
    directory = suite_runs_dir(project_root, project)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = str(run_record["observed_at"]).replace(":", "").replace("-", "")
    revision = str(run_record.get("revision") or "unknown")[:12]
    path = directory / f"{stamp}-{revision}.json"
    _write_json(path, dict(run_record))
    return path


def record_waiver(
    project_root: str | Path,
    project: str,
    *,
    who: str,
    why: str,
) -> Path:
    """Write a waiver that lifts the hold on a bad run.

    A waiver is a record like a run, carrying its kind and the person and
    reason; a hold is lifted when a waiver's observation time is later than
    the latest failing run's.
    """
    directory = suite_runs_dir(project_root, project)
    directory.mkdir(parents=True, exist_ok=True)
    observed_at = _observed_at()
    stamp = observed_at.replace(":", "").replace("-", "")
    path = directory / f"{stamp}-waiver.json"
    payload = {
        "kind": WAIVER_KIND,
        "who": who,
        "why": why,
        "observed_at": observed_at,
    }
    _write_json(path, payload)
    return path


def _latest_record(project_root: str | Path, project: str) -> dict[str, Any] | None:
    """The record with the latest observation time, run or waiver, or ``None``."""
    directory = suite_runs_dir(project_root, project)
    if not directory.is_dir():
        return None
    latest: dict[str, Any] | None = None
    for path in sorted(directory.glob("*.json")):
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            continue
        if latest is None or str(payload.get("observed_at") or "") > str(
            latest.get("observed_at") or ""
        ):
            latest = dict(payload)
    return latest


def hold_reason(
    project_root: str | Path,
    tier: str,
    project: str | None = None,
) -> str | None:
    """Why a ``none`` or ``light`` promotion must wait, or ``None`` to proceed.

    A hold exists only while the latest recorded run failed to collect or
    overran its budget and no later waiver has lifted it. Any other tier, a
    project with no records, a latest run that passed, and a latest record that
    is a waiver all return ``None``. The sentence names the revision, when the
    run was observed, and which of the two failures it recorded, so a reader
    sees what must be answered rather than only that something is holding.
    """
    if tier not in HELD_TIERS:
        return None
    if project is None:
        project = _project_for_root(project_root)
    if project is None:
        return None
    latest = _latest_record(project_root, project)
    if latest is None or latest.get("kind") == WAIVER_KIND:
        return None
    if not latest.get("collection_failed") and not latest.get("over_budget"):
        return None
    failures = []
    if latest.get("collection_failed"):
        failures.append("failed to collect")
    if latest.get("over_budget"):
        failures.append("overran its budget")
    return (
        f"the project's latest suite run at {latest.get('revision')} "
        f"(observed {latest.get('observed_at')}) "
        f"{' and '.join(failures)}; a tier {tier!r} promotion waits until a "
        "passing run is recorded or the lead waives it"
    )


def _project_for_root(project_root: str | Path) -> str | None:
    """The mounted project whose docs tree is ``project_root``'s, or ``None``.

    Used only when a caller passes no project name: a mounted checkout is
    matched by its resolved docs directory, so the resolver names the project
    rather than the caller restating it.
    """
    from reckon.flight import mounted_project_docs

    target = (Path(project_root) / "docs").resolve()
    for project, docs in mounted_project_docs().items():
        if Path(docs).resolve() == target:
            return project
    return None


def _read_json(path: Path) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# The atomic JSON writer. Imports the shared helper at call time so the module
# imports without pulling the run registry in for callers that only read.
def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    from reckon.crew.runs import _write_json as write

    write(path, payload)
