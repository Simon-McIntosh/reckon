"""A worker launched inside an allocation receives its host contract.

Fixture :class:`reckon.host.HostFacts` objects select both sides, so these cases
never read the process's real cgroup or mount table.  The guarded behavior has
two coupled surfaces: the effective launch ``PATH`` starts with the scheduler
shims, and the composed prompt states the measured host facts and durable-log
location.  Outside an allocation, both additions are absent.
"""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path

import pytest

from reckon import host as host_module
from reckon._backends import LaunchPlan
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from reckon.host import HostFacts
from reckon.nested_launch import real_binary, shim_directory

dispatch_module = import_module("reckon.crew.dispatch")

DECLARED_MUTATION = "remove the shim-directory PATH prepend"
MUTATION_ENV = "RECKON_TEST_REMOVE_SHIM_PATH_PREPEND"
NODE = "98dci4-clu-2058"
JOB = "1277272"
SCRATCH = "/tmp"  # noqa: S108 — fixture path, never written


def _facts(*, inside: bool) -> HostFacts:
    return HostFacts(
        in_allocation=inside,
        job_id=JOB if inside else None,
        step_id="batch" if inside else None,
        node=NODE if inside else None,
        tmp_filesystem="xfs",
        home_filesystem="gpfs",
        tmp_is_node_local=True,
        reason="" if inside else "no-slurm-job-id-in-the-environment",
        sources={},
    )


def _node() -> TaskNode:
    return TaskNode(
        id="worker-path",
        goal="carry the host contract into a worker launch",
        plan="compute-worker",
        section="host-contract",
        role="implement",
        done_when="inside and outside fixture host facts select opposite contracts",
        write_paths=["reckon/crew/dispatch.py", "reckon/crew/prompts.py"],
        time_budget="20m",
    )


def _prompt(facts: HostFacts, run_directory: Path) -> str:
    return compose_prompt(
        node=_node(),
        project="reckon",
        worktree="/repo/worktrees/worker-path",
        working_directory="/repo/worktrees/worker-path",
        manifest_path=str(run_directory / "manifest.md"),
        time_budget="20m",
        needs_help_after_failures=2,
        host_line=dispatch_module._worker_host_line(facts, run_directory),
    )


def _without_shim_prepend(environment: dict[str, str] | None = None) -> str:
    """The declared mutant: return the inherited search path unchanged."""
    merged = {**os.environ, **(environment or {})}
    return str(merged.get("PATH") or os.defpath)


def _search_path(environment: dict[str, str]) -> str:
    if os.environ.get(MUTATION_ENV) == DECLARED_MUTATION:
        return _without_shim_prepend(environment)
    return dispatch_module.launch_search_path(environment)


def _executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_inside_allocation_launch_path_starts_with_the_shim_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = _facts(inside=True)
    monkeypatch.setattr(host_module, "host_facts", lambda: facts)
    real_bin = tmp_path / "real-bin"
    worker = _executable(real_bin, "worker")
    real_srun = _executable(real_bin, "srun")
    inherited = str(real_bin)

    searched = _search_path({"PATH": inherited})

    entries = searched.split(os.pathsep)
    assert entries[0] == str(shim_directory())
    assert entries[1:] == [inherited]
    assert real_binary("srun", shim_directory(), searched) == str(real_srun)

    plan = dispatch_module.resolve_launch_executable(
        LaunchPlan(
            backend="fixture",
            dialect="fixture",
            argv=[worker.name],
            cwd=str(tmp_path),
            stdin_text="",
            environment={"PATH": inherited},
            final_message_path=None,
            resumed_session=None,
        )
    )
    assert plan.argv[0] == str(worker)
    launched = dispatch_module._launch_environment(plan.environment, facts=facts)
    assert launched["PATH"].split(os.pathsep)[0] == str(shim_directory())
    spec = dispatch_module._supervisor_spec(
        run_id="worker-path",
        run_directory=tmp_path / "run",
        repo_root=tmp_path,
        worktree=tmp_path / "worktree",
        plan=plan,
        prompt_path=tmp_path / "prompt.txt",
        log_path=tmp_path / "stream.jsonl",
        stderr_path=tmp_path / "stderr.log",
        facts=facts,
    )
    assert spec["plan"]["environment"]["PATH"].split(os.pathsep)[0] == str(
        shim_directory()
    )


def test_inside_allocation_prompt_names_the_host_and_storage_contract(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "runs" / "worker-path"
    prompt = " ".join(_prompt(_facts(inside=True), run_directory).split())

    assert f"node {NODE}" in prompt
    assert f"job {JOB}" in prompt
    assert f"{SCRATCH} is node-local" in prompt
    assert "do not use srun, sbatch or salloc" in prompt
    assert "the worker already runs on the node" in prompt
    assert f"logs a later reader needs go under {run_directory}" in prompt


def test_outside_allocation_adds_neither_shims_nor_host_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = _facts(inside=False)
    monkeypatch.setattr(host_module, "host_facts", lambda: facts)
    real_bin = tmp_path / "real-bin"
    _executable(real_bin, "worker")
    inherited = str(real_bin)

    searched = dispatch_module.launch_search_path({"PATH": inherited})
    launched = dispatch_module._launch_environment({"PATH": inherited}, facts=facts)
    prompt = _prompt(facts, tmp_path / "runs" / "worker-path")

    assert searched == inherited
    assert launched["PATH"] == inherited
    assert str(shim_directory()) not in searched.split(os.pathsep)
    assert "HOST — ALLOCATION" not in prompt
    assert "do not use srun, sbatch or salloc" not in prompt
