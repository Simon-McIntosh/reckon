"""A worker launched inside an allocation receives its host contract.

Fixture :class:`reckon.host.HostFacts` objects select both sides, so these cases
never read the process's real cgroup or mount table.  The guarded behavior has
two coupled surfaces: the effective launch ``PATH`` starts with the worker
shims, and the composed prompt states the measured host facts and durable-log
location.  The git shim is first on the path in either placement; the scheduler
shims, and the host line with them, are present only inside an allocation.
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
from reckon.worker_git_shim import worker_shim_directory

dispatch_module = import_module("reckon.crew.dispatch")

DECLARED_MUTATION = "remove the shim-directory PATH prepend"
MUTATION_ENV = "RECKON_TEST_REMOVE_SHIM_PATH_PREPEND"
PERSISTENCE_MUTATION = "restore the full-environment merge in persisted launch data"
PERSISTENCE_MUTATION_ENV = "RECKON_TEST_PERSIST_FULL_ENVIRONMENT"
SENTINEL = "RECKON_TEST_SECRET"
SENTINEL_VALUE = "do-not-persist"
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
    assert entries[0] == str(worker_shim_directory())
    assert entries[1] == str(shim_directory())
    assert entries[2:] == [inherited]
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
    persisted = dispatch_module._persisted_worker_environment(
        plan.environment, facts=facts
    )
    assert persisted["PATH"].split(os.pathsep)[0] == str(worker_shim_directory())
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
        worker_shim_directory()
    )


def test_persisted_worker_environments_exclude_dispatcher_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = _facts(inside=True)
    monkeypatch.setenv(SENTINEL, SENTINEL_VALUE)
    if os.environ.get(PERSISTENCE_MUTATION_ENV) == PERSISTENCE_MUTATION:
        persisted_environment = dispatch_module._persisted_worker_environment

        def restore_full_environment_merge(environment=None, *, facts=None):
            return {
                **os.environ,
                **persisted_environment(environment, facts=facts),
            }

        monkeypatch.setattr(
            dispatch_module,
            "_persisted_worker_environment",
            restore_full_environment_merge,
        )

    inherited = str(tmp_path / "real-bin")
    overlay = {"PATH": inherited, "PLAN_ONLY": "retained"}
    plan = LaunchPlan(
        backend="fixture",
        dialect="fixture",
        argv=["worker"],
        cwd=str(tmp_path),
        stdin_text="",
        environment=overlay,
        final_message_path=None,
        resumed_session=None,
    )
    spec = dispatch_module._supervisor_spec(
        run_id="worker-persistence",
        run_directory=tmp_path / "run-persistence",
        repo_root=tmp_path,
        worktree=tmp_path / "worktree-persistence",
        plan=plan,
        prompt_path=tmp_path / "prompt-persistence.txt",
        log_path=tmp_path / "stream-persistence.jsonl",
        stderr_path=tmp_path / "stderr-persistence.log",
        facts=facts,
    )
    directive_environment = dispatch_module._persisted_worker_environment(
        {}, facts=facts
    )

    assert spec["plan"]["environment"]["PATH"].split(os.pathsep)[0] == str(
        worker_shim_directory()
    )
    assert spec["plan"]["environment"]["PLAN_ONLY"] == "retained"
    assert SENTINEL not in spec["plan"]["environment"]
    assert set(spec["plan"]["environment"]) == {"PATH", "PLAN_ONLY"}
    assert directive_environment["PATH"].split(os.pathsep)[0] == str(
        worker_shim_directory()
    )
    assert SENTINEL not in directive_environment
    assert set(directive_environment) == {"PATH"}


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


def test_outside_allocation_carries_the_git_shim_but_no_scheduler_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = _facts(inside=False)
    monkeypatch.setattr(host_module, "host_facts", lambda: facts)
    real_bin = tmp_path / "real-bin"
    _executable(real_bin, "worker")
    inherited = str(real_bin)
    overlay = {"PATH": inherited, "PLAN_ONLY": "retained"}

    searched = dispatch_module.launch_search_path(overlay)
    persisted = dispatch_module._persisted_worker_environment(overlay, facts=facts)
    prompt = _prompt(facts, tmp_path / "runs" / "worker-path")

    plan = LaunchPlan(
        backend="fixture",
        dialect="fixture",
        argv=["worker"],
        cwd=str(tmp_path),
        stdin_text="",
        environment=overlay,
        final_message_path=None,
        resumed_session=None,
    )
    spec = dispatch_module._supervisor_spec(
        run_id="worker-path-outside",
        run_directory=tmp_path / "run-outside",
        repo_root=tmp_path,
        worktree=tmp_path / "worktree-outside",
        plan=plan,
        prompt_path=tmp_path / "prompt-outside.txt",
        log_path=tmp_path / "stream-outside.jsonl",
        stderr_path=tmp_path / "stderr-outside.log",
        facts=facts,
    )

    # Out of an allocation the git shim is still first on the path — it refuses a
    # mutating verb aimed at another checkout, which is a hazard on any host —
    # while the scheduler shims, which have no scheduler to refuse, are absent.
    assert searched.split(os.pathsep)[0] == str(worker_shim_directory())
    assert str(shim_directory()) not in searched.split(os.pathsep)
    assert persisted["PATH"] == searched
    assert persisted["PLAN_ONLY"] == "retained"
    assert spec["plan"]["environment"]["PATH"] == searched
    assert spec["plan"]["environment"]["PLAN_ONLY"] == "retained"
    assert set(dispatch_module._persisted_worker_environment({}, facts=facts)) == {
        "PATH"
    }
    assert "HOST — ALLOCATION" not in prompt
    assert "do not use srun, sbatch or salloc" not in prompt
