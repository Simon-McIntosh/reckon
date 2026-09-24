"""Tests for the host-facts probe.

Every case feeds fixture files: the cgroup line, the cgroup tree and the mount
table are all passed in, and the mount-table predicate the probe composes is
stubbed from the same fixture, so no case here reads the real ``/proc`` or a
real filesystem. The declared mutation -- the cgroup check replaced by a
``SLURM_JOB_ID`` environment check -- is applied in memory by
:func:`apply_declared_mutation`, and the environment-only case reads true under
it, so the guard is shown to be what makes that case false.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path

from reckon import host as host_module

# Imported by name because ``reckon.crew`` exports a ``dispatch`` *function* of
# the same name, so ``import reckon.crew.dispatch as ...`` binds that function.
dispatch_module = import_module("reckon.crew.dispatch")

# The mount point the probe is asked about: the node's own scratch.
TMP = "/tmp"  # noqa: S108 — fixture mount point, never opened

# A process inside the job: the job cgroup is an ancestor of the process's own.
JOB_CGROUP = "0::/system.slice/slurmstepd.scope/job_1277272/step_batch/user/task_0"
JOB_TREE = "system.slice/slurmstepd.scope/job_1277272/step_batch/user/task_0"
JOB_ID = "1277272"

# A shell into the node inherits SLURM_JOB_ID while running outside the job.
INHERITED_CGROUP = "0::/user.slice/user-39486.slice/session-11.scope"
INHERITED_TREE = "user.slice/user-39486.slice/session-11.scope"

SLURM_ENV = {
    "SLURM_JOB_ID": JOB_ID,
    "SLURMD_NODENAME": "98dci4-clu-2058",
    "SLURM_STEP_ID": "batch",
}

GPFS_HOME = "/home/ITER/mcintos"
MACHINE_MOUNTS = [("/", "xfs"), (TMP, "xfs"), (GPFS_HOME, "gpfs")]
TMPFS_MOUNTS = [("/", "xfs"), (TMP, "tmpfs"), (GPFS_HOME, "gpfs")]
SHARED_TMP_MOUNTS = [("/", "xfs"), (TMP, "gpfs"), (GPFS_HOME, "gpfs")]

NO_JOB_REASON = "no-slurm-job-id-in-the-environment"
INHERITED_REASON = "environment-inherited-outside-the-job-cgroup"

# The mutation this node declares. The runner that produces the red log imports
# this constant, so the log's first line is the text matching the declaration.
DECLARED_MUTATION = "replace the cgroup check with a SLURM_JOB_ID environment check"

_MUTATION_FROM = (
    "    if _cgroup_names_job(components, job_id):\n"
    "        return True\n"
    '    return bounded is not None and f"job_{job_id}" in bounded.parts\n'
)
_MUTATION_TO = "    return job_id is not None\n"


def apply_declared_mutation(source: str) -> str:
    """The probe's source with the declared mutation applied.

    Raises when the cgroup check cannot be found, rather than returning the
    source unchanged: a mutation that did nothing would report the guard as
    load-bearing when nothing had been changed.
    """
    if _MUTATION_FROM not in source:
        raise AssertionError(
            "the declared mutation's target has changed: the cgroup check must "
            "be the body of _inside_the_job"
        )
    return source.replace(_MUTATION_FROM, _MUTATION_TO)


def cgroup_tree(tmp_path: Path, relative: str, *, bounded_at: int) -> Path:
    """A cgroup tree for ``relative``, bounded ``bounded_at`` components deep.

    Only that one level carries ``memory.max``, so the walk upward has exactly
    one finite answer and the level it reports is chosen by the fixture rather
    than by the host.
    """
    root = tmp_path / "cgroup"
    parts = [part for part in relative.strip("/").split("/") if part]
    for depth in range(1, len(parts) + 1):
        root.joinpath(*parts[:depth]).mkdir(parents=True, exist_ok=True)
    bounded = root.joinpath(*parts[:bounded_at])
    (bounded / "memory.max").write_text("137438953472\n", encoding="utf-8")
    (bounded / "memory.current").write_text("4096\n", encoding="utf-8")
    return root


def mount_table_file(tmp_path: Path, entries: list[tuple[str, str]]) -> Path:
    """A minimal mount table: one line per ``(mount point, filesystem)``."""
    lines = [
        f"30 {index} 0:1 / {mount} rw shared:1 - {filesystem} src rw\n"
        for index, (mount, filesystem) in enumerate(entries)
    ]
    path = tmp_path / "mountinfo"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def mount_for(entries: list[tuple[str, str]], target: str) -> str | None:
    """The filesystem covering ``target`` in ``entries``.

    Its own short answer rather than a call into the code under test: a stub
    built from the probe's own reader would agree with it by construction and
    would stop standing in for it.
    """
    best: tuple[int, int, str] | None = None
    for index, (mount, filesystem) in enumerate(entries):
        point = os.path.normpath(mount)
        covered = target == point or target.startswith(point.rstrip("/") + "/")
        if covered and (best is None or (len(point), index) >= best[:2]):
            best = (len(point), index, filesystem)
    return best[2] if best else None


@contextmanager
def stubbed_tmpfs(entries: list[tuple[str, str]]) -> Iterator[None]:
    """Stand-in for the mount-table predicate, answering from fixture entries."""
    saved = dispatch_module._path_is_tmpfs
    dispatch_module._path_is_tmpfs = lambda path: (
        mount_for(entries, os.path.normpath(str(path))) in {"tmpfs", "ramfs"}
    )
    try:
        yield
    finally:
        dispatch_module._path_is_tmpfs = saved


def read_host_facts(
    tmp_path: Path,
    *,
    environ: dict[str, str],
    self_cgroup: str,
    tree: str,
    bounded_at: int,
    mounts: list[tuple[str, str]],
    module: object = host_module,
    tmp_path_argument: str = TMP,
    home_path: str = GPFS_HOME,
):
    """One probe reading against fixtures, with the mount predicate stubbed."""
    with stubbed_tmpfs(mounts):
        return module.host_facts(
            environ=environ,
            self_cgroup=self_cgroup,
            cgroup_root=cgroup_tree(tmp_path, tree, bounded_at=bounded_at),
            mountinfo_path=mount_table_file(tmp_path, mounts),
            tmp_path=tmp_path_argument,
            home_path=home_path,
        )


def environment_only_case(module: object, tmp_path: Path):
    """The environment-only reading: the job id is set, the cgroup names no job."""
    return read_host_facts(
        tmp_path,
        environ=SLURM_ENV,
        self_cgroup=INHERITED_CGROUP,
        tree=INHERITED_TREE,
        bounded_at=1,
        mounts=MACHINE_MOUNTS,
        module=module,
    )


def load_declared_mutant(tmp_path: Path) -> object:
    """Import the probe with the declared mutation applied.

    The mutated source is written outside the package and imported under its
    own name, so the mutated image is measured and the reviewed module on disk
    is never edited.
    """
    source = Path(host_module.__file__).read_text(encoding="utf-8")
    mutated = apply_declared_mutation(source)
    assert mutated != source
    directory = tmp_path / "mutant"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "host_mutant.py"
    path.write_text(mutated, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("reckon_host_mutant", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["reckon_host_mutant"] = module
    spec.loader.exec_module(module)
    return module


def test_inside_the_job_cgroup_reads_true(tmp_path: Path) -> None:
    facts = read_host_facts(
        tmp_path,
        environ=SLURM_ENV,
        self_cgroup=JOB_CGROUP,
        tree=JOB_TREE,
        bounded_at=3,
        mounts=MACHINE_MOUNTS,
    )
    assert facts.in_allocation is True
    assert facts.reason == ""
    assert facts.job_id == JOB_ID
    assert facts.node == "98dci4-clu-2058"
    assert facts.step_id == "batch"


def test_environment_inherited_outside_the_job_cgroup_reads_false(
    tmp_path: Path,
) -> None:
    facts = environment_only_case(host_module, tmp_path)
    assert facts.in_allocation is False, facts.reason
    assert facts.reason == INHERITED_REASON


def test_no_slurm_job_id_at_all_reads_false(tmp_path: Path) -> None:
    facts = read_host_facts(
        tmp_path,
        environ={},
        self_cgroup=INHERITED_CGROUP,
        tree=INHERITED_TREE,
        bounded_at=1,
        mounts=MACHINE_MOUNTS,
    )
    assert facts.in_allocation is False
    assert facts.job_id is None
    assert facts.reason == NO_JOB_REASON


def test_gpfs_home_beside_a_node_local_tmp(tmp_path: Path) -> None:
    facts = read_host_facts(
        tmp_path,
        environ=SLURM_ENV,
        self_cgroup=JOB_CGROUP,
        tree=JOB_TREE,
        bounded_at=3,
        mounts=MACHINE_MOUNTS,
    )
    assert facts.tmp_filesystem == "xfs"
    assert facts.home_filesystem == "gpfs"
    assert facts.tmp_is_node_local is True


def test_a_shared_tmp_is_not_node_local(tmp_path: Path) -> None:
    facts = read_host_facts(
        tmp_path,
        environ=SLURM_ENV,
        self_cgroup=JOB_CGROUP,
        tree=JOB_TREE,
        bounded_at=3,
        mounts=SHARED_TMP_MOUNTS,
    )
    assert facts.tmp_filesystem == "gpfs"
    assert facts.tmp_is_node_local is False


def test_a_tmpfs_tmp_is_node_local(tmp_path: Path) -> None:
    facts = read_host_facts(
        tmp_path,
        environ=SLURM_ENV,
        self_cgroup=JOB_CGROUP,
        tree=JOB_TREE,
        bounded_at=3,
        mounts=TMPFS_MOUNTS,
    )
    assert facts.tmp_filesystem == "tmpfs"
    assert facts.tmp_is_node_local is True


def test_every_fact_names_the_source_it_was_read_from(tmp_path: Path) -> None:
    facts = read_host_facts(
        tmp_path,
        environ=SLURM_ENV,
        self_cgroup=JOB_CGROUP,
        tree=JOB_TREE,
        bounded_at=3,
        mounts=MACHINE_MOUNTS,
    )
    reported = facts.facts()
    assert set(reported) == set(host_module.FACTS)
    sources = {key: entry["source"] for key, entry in reported.items()}
    assert all(sources.values()), sources
    assert reported["in_allocation"]["value"] is True
    assert reported["job_id"]["value"] == JOB_ID
    assert sources["tmp_filesystem"] == f"mount table: {tmp_path / 'mountinfo'}"
    assert sources["job_id"] == "environment: SLURM_JOB_ID"
    assert sources["tmp_is_node_local"].startswith("reckon.crew.dispatch.")


def test_the_declared_mutation_makes_the_environment_only_case_read_true(
    tmp_path: Path,
) -> None:
    """The guard is load-bearing: without it the inherited shell reads inside.

    The mutant's only difference from the reviewed module is that the cgroup
    check is an environment check, and the environment-only case it then gets
    wrong is the one the real module is asserted to read false above.
    """
    mutant = load_declared_mutant(tmp_path)
    facts = environment_only_case(mutant, tmp_path)
    assert facts.in_allocation is True


def test_the_real_module_and_the_mutant_disagree_on_the_same_fixture(
    tmp_path: Path,
) -> None:
    real = environment_only_case(host_module, tmp_path / "real")
    mutated = environment_only_case(
        load_declared_mutant(tmp_path / "mutant"), tmp_path / "mutant"
    )
    assert real.in_allocation is False
    assert real.reason == INHERITED_REASON
    assert mutated.in_allocation is True
    assert mutated.reason == ""
