"""An in-allocation worker may still submit a separate partition job.

The HOST line forbids only a step inside this allocation: an ``srun`` or
``salloc`` opened in this job. Heavy work that does not belong here is
discharged as a separate partition job, submitted with ``sbatch`` when the
node's done-when names one, and the line says that a nested ``srun`` runs as a
step of the held job whatever partition it names. The line must name the route
with its verb and the step fact, so it never contradicts the role digests or a
done-when that requires a separate partition job.
"""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path

import pytest

from reckon import nested_launch as nested
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from reckon.host import HostFacts

dispatch_module = import_module("reckon.crew.dispatch")

# The declaration this file pins, captured once at import so a route changed
# later in the process cannot move the expectation the prompt is read against.
DECLARED_ROUTE = nested.PARTITION_ROUTE
DECLARED_SENTENCE = nested.NESTED_SRUN_SENTENCE

# The declared mutation, printed verbatim as the red log's first line.
DECLARED_MUTATION = (
    "restore the unconditional 'do not use srun, sbatch or salloc' prohibition "
    "in _worker_host_line; the new nested-launch test must turn red"
)

MUTATION_ENV = "RECKON_HOST_LINE_NEGATIVE_CONTROL"

# Environment gate for the route mutation shared with the refusal cases: drop
# the partition route's verb clause in memory, leaving the srun sentence intact.
ROUTE_MUTATION_ENV = "RECKON_PARTITION_ROUTE_NEGATIVE_CONTROL"

NODE = "98dci4-clu-2058"
JOB = "1277272"
SCRATCH = "/tmp"  # noqa: S108 — fixture path, never written


def _facts() -> HostFacts:
    return HostFacts(
        in_allocation=True,
        job_id=JOB,
        step_id="batch",
        node=NODE,
        tmp_filesystem="xfs",
        home_filesystem="gpfs",
        tmp_is_node_local=True,
        reason="",
        sources={},
    )


def _node() -> TaskNode:
    return TaskNode(
        id="host-line-nested-launch",
        goal="submit a separate partition job from inside the allocation",
        plan="compute-worker",
        section="host-contract",
        role="implement",
        done_when="a betelgeuse H200 job is submitted with RECKON_ALLOW_NESTED_LAUNCH=1",
        write_paths=["reckon/crew/dispatch.py"],
        time_budget="40m",
    )


def _prompt(facts: HostFacts, run_directory: Path) -> str:
    return compose_prompt(
        node=_node(),
        project="reckon",
        worktree="/repo/worktrees/host-line",
        working_directory="/repo/worktrees/host-line",
        manifest_path=str(run_directory / "manifest.md"),
        time_budget="40m",
        needs_help_after_failures=2,
        host_line=dispatch_module._worker_host_line(facts, run_directory),
    )


def _unconditional_prohibition(facts: HostFacts, run_directory: str | Path) -> str:
    """The declared mutant: the line that forbade every scheduler verb."""
    if not facts.in_allocation:
        return ""
    node = facts.node or "unknown"
    job = facts.job_id or "unknown"
    tmp_clause = (
        f"{SCRATCH} is node-local"
        if facts.tmp_is_node_local
        else f"{SCRATCH} is not node-local"
    )
    return (
        f"HOST — ALLOCATION: node {node}; job {job}; {tmp_clause}; do not use "
        "srun, sbatch or salloc because the worker already runs on the node; "
        f"logs a later reader needs go under {run_directory}."
    )


@pytest.fixture(autouse=True)
def _declared_negative_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation when its environment variable is set."""
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(
            dispatch_module, "_worker_host_line", _unconditional_prohibition
        )


@pytest.fixture(autouse=True)
def _declared_route_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the partition route's verb clause in memory when the control asks.

    Applied after this module has captured the declaration, so the composed
    host line no longer carries the declared route and the case below fails on
    its own text assertion rather than on a comparison against a changed value.
    """
    if os.environ.get(ROUTE_MUTATION_ENV) == "1":
        monkeypatch.setattr(nested, "PARTITION_ROUTE", "a separate partition job")


def test_in_allocation_prompt_permits_a_separate_partition_job(
    tmp_path: Path,
) -> None:
    """The prompt forbids an in-place step and names the route out with its verb.

    A worker that reads only a prohibition against every scheduler verb
    concludes that no submission route exists and blocks a done-when that
    requires a separate partition job. The line must forbid a step inside this
    allocation and name the sanctioned route with the verb that reaches it.
    """
    run_directory = tmp_path / "runs" / "host-line"
    prompt = " ".join(_prompt(_facts(), run_directory).split())

    # The prohibition is scoped to this job, not to every scheduler verb.
    assert "the work runs in place" in prompt
    assert "do not open an srun or salloc step in this job" in prompt
    # The line carries the declared route, which names its submitting verb,
    # verbatim, and separately says an srun stays a step of the held job.
    assert DECLARED_ROUTE in prompt
    assert nested.PARTITION_ROUTE_VERB in DECLARED_ROUTE
    assert DECLARED_SENTENCE in prompt
    assert "betelgeuse" in prompt
    assert "RECKON_ALLOW_NESTED_LAUNCH=1" in prompt
    # The node, job, scratch and log clauses are kept.
    assert f"node {NODE}" in prompt
    assert f"job {JOB}" in prompt
    assert f"{SCRATCH} is node-local" in prompt
    assert f"a later reader needs go under {run_directory}" in prompt
    # The old unconditional wording, which contradicted the done-when, is gone.
    assert "do not use srun, sbatch or salloc" not in prompt


def test_the_mutant_forbids_every_scheduler_verb(tmp_path: Path) -> None:
    """The declared mutation is the unconditional prohibition it replaces."""
    run_directory = tmp_path / "runs" / "host-line"
    line = _unconditional_prohibition(_facts(), run_directory)

    assert "do not use srun, sbatch or salloc" in line
    assert "RECKON_ALLOW_NESTED_LAUNCH" not in line


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(DECLARED_MUTATION)
    os.environ[MUTATION_ENV] = "1"
    code = pytest.main(["-p", "no:cacheprovider", "-q", str(Path(__file__))])
    print(f"EXIT={code}")
    raise SystemExit(code)
