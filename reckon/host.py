"""Facts about the host this process runs on.

Two answers here decide whether a compute-node rule applies, and each already
has one reader in the tree. This module composes those readers rather than
adding a second answer that could disagree with them:

* whether the process sits inside the held SLURM allocation. The process's own
  cgroup line is the authority, because ``SLURM_JOB_ID`` is inherited by shells
  that are not inside the job — an ssh session into the node carries the
  variable while running in ``user.slice``. The cgroup reading is
  :mod:`reckon.crew.summary`'s, and the walk to a bounded ancestor is its
  ``_user_slice_dir``.
* whether a path lives on storage only this node can see, which is
  :func:`reckon.crew.dispatch._is_node_local_path` (backed by
  :func:`reckon.crew.dispatch._path_is_tmpfs`, both read from
  ``/proc/self/mountinfo``). The filesystem types are read from that same mount
  table so a reader sees *why* the answer is what it is.

Nothing here runs a scheduler command, so a call costs a few file reads and is
cheap enough to take on a login node.

One direction worth knowing: a block-backed local scratch (an ``xfs`` ``/tmp``
on the node's own disk) is node-local, and the mount table says so by naming a
filesystem that is not shared. The tmpfs/runtime-directory predicate alone
under-reports that case, so the mount's own type widens it rather than
narrowing it — the two can only differ where the predicate is too narrow.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

from reckon.crew.summary import LOGIN_CGROUP_ROOT, SELF_CGROUP_PATH, _user_slice_dir

# Imported by name because ``reckon.crew`` exports a ``dispatch`` *function* of
# the same name, so ``from reckon.crew import dispatch`` and
# ``import reckon.crew.dispatch as dispatch`` both bind that function rather
# than the module these readers live in.
_dispatch = import_module("reckon.crew.dispatch")

# The mount table the filesystem-type and node-local answers both come from.
MOUNTINFO_PATH = Path("/proc/self/mountinfo")

# Filesystem types whose bytes belong to another host. A path on one of these
# is not node-local whatever else is true of them.
_SHARED_FILESYSTEMS = frozenset(
    {"gpfs", "nfs", "nfs4", "lustre", "ceph", "fuse", "fuseblk", "9p"}
)

# Reasons ``in_allocation`` is false. The middle one is what an inherited
# environment produces: the variable is present and the cgroup does not name
# the job — exactly the case an environment-only check gets wrong.
_NO_JOB_ID = "no-slurm-job-id-in-the-environment"
_ENVIRONMENT_ONLY = "environment-inherited-outside-the-job-cgroup"
_UNREADABLE_CGROUP = "own-cgroup-line-unreadable"

_SOURCE_CGROUP_WALK = (
    "reckon.crew.summary._user_slice_dir over the process's own cgroup line"
)

# The facts a reader is promised, in the order they are reported.
FACTS = (
    "in_allocation",
    "job_id",
    "step_id",
    "node",
    "tmp_filesystem",
    "home_filesystem",
    "tmp_is_node_local",
)


@dataclass(frozen=True)
class HostFacts:
    """One reading of the host's placement and storage.

    ``sources`` names, for each fact, where the value was read from, so a
    reader can tell a measured fact from one that fell back to a default.
    ``reason`` is empty inside the allocation and otherwise says why not.
    """

    in_allocation: bool
    job_id: str | None
    step_id: str | None
    node: str | None
    tmp_filesystem: str | None
    home_filesystem: str | None
    tmp_is_node_local: bool
    reason: str = ""
    sources: Mapping[str, str] = field(default_factory=dict)

    def facts(self) -> dict[str, dict[str, Any]]:
        """Every fact beside the source it was read from."""
        return {
            key: {"value": getattr(self, key), "source": self.sources.get(key, "")}
            for key in FACTS
        }


def _own_cgroup_components(self_cgroup_text: str | None) -> list[str] | None:
    """The path components of the process's own cgroup line, or None.

    A cgroup v2 line reads ``0::/a/b/c``; a v1 line names a hierarchy the
    compute rules do not use, so only the ``0::`` record is read.
    """
    if self_cgroup_text is None:
        return None
    for line in str(self_cgroup_text).splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return [segment for segment in parts[2].strip().split("/") if segment]
    return None


def _cgroup_names_job(components: list[str] | None, job_id: str) -> bool:
    """Whether the process's own cgroup path names ``job_id``'s job."""
    return bool(components) and f"job_{job_id}" in components


def _inside_the_job(
    components: list[str] | None, job_id: str, bounded: Path | None
) -> bool:
    """Whether the process sits inside ``job_id``'s cgroup.

    The cgroup line is the authority. A ``SLURM_JOB_ID`` in the environment is
    not, because a shell outside the job inherits the variable.
    """
    if _cgroup_names_job(components, job_id):
        return True
    return bounded is not None and f"job_{job_id}" in bounded.parts


def _mount_table(mountinfo_path: str | Path) -> list[tuple[Path, str]] | None:
    """Every mount in the mount table as ``(mount point, filesystem type)``."""
    try:
        text = Path(mountinfo_path).read_text(encoding="utf-8")
    except OSError:
        return None
    mounts: list[tuple[Path, str]] = []
    for line in text.splitlines():
        fields, separator, trailing = line.partition(" - ")
        if not separator:
            continue
        parts = fields.split()
        trailing_parts = trailing.split()
        if len(parts) < 5 or not trailing_parts:
            continue
        mounts.append((Path(parts[4].replace("\\040", " ")), trailing_parts[0]))
    return mounts


def _filesystem_type(
    target: str | Path, mounts: list[tuple[Path, str]] | None
) -> str | None:
    """The filesystem type covering ``target``, or None when it is not known.

    The longest mount point that is a prefix of the path wins, and a later line
    breaks a tie, because a mount over an existing one is the one whose bytes a
    reader actually reaches. Nothing is resolved against the filesystem: the
    path is normalised as text, so the answer never depends on the tree being
    readable now.
    """
    if mounts is None:
        return None
    normalised = os.path.normpath(str(target))
    best: tuple[int, int, str] | None = None
    for index, (mount, filesystem) in enumerate(mounts):
        point = os.path.normpath(str(mount))
        if normalised == point or normalised.startswith(point.rstrip("/") + "/"):
            candidate = (len(point), index, filesystem)
            if best is None or candidate[:2] >= best[:2]:
                best = candidate
    return best[2] if best else None


def _is_node_local(target: str | Path, filesystem: str | None) -> bool:
    """Whether a path is on storage only this node can see.

    The tree's own readers answer for the memory-backed and per-user runtime
    cases; a filesystem that is plainly not shared widens it to the
    block-backed local scratch those readers do not name.
    """
    if _dispatch._path_is_tmpfs(target):
        return True
    if _dispatch._is_node_local_path(target):
        return True
    return filesystem is not None and filesystem not in _SHARED_FILESYSTEMS


def host_facts(
    *,
    environ: Mapping[str, str] | None = None,
    self_cgroup: str | None = None,
    cgroup_root: str | Path | None = None,
    mountinfo_path: str | Path | None = None,
    tmp_path: str | Path = "/tmp",  # noqa: S108 — the node's own scratch path
    home_path: str | Path | None = None,
) -> HostFacts:
    """Read the host's placement and storage in one call.

    Every input the probe reads is a named argument so a test can feed fixture
    files and no test has to read the real cgroup line, mount table or
    filesystem. The defaults are the real paths.
    """
    env = os.environ if environ is None else environ
    text = self_cgroup
    if text is None:
        try:
            text = SELF_CGROUP_PATH.read_text(encoding="utf-8")
            cgroup_source = f"file: {SELF_CGROUP_PATH}"
        except OSError:
            text = None
            cgroup_source = f"file: {SELF_CGROUP_PATH} (unreadable)"
    else:
        cgroup_source = "own cgroup line passed in"

    root = Path(cgroup_root) if cgroup_root is not None else LOGIN_CGROUP_ROOT
    components = _own_cgroup_components(text)
    # ``_user_slice_dir`` falls back to the real cgroup file when handed None,
    # so it is only consulted once the line has actually been read.
    bounded = _user_slice_dir(root, text) if text is not None else None

    job_id = env.get("SLURM_JOB_ID") or env.get("SLURM_JOBID") or None
    node = env.get("SLURMD_NODENAME") or env.get("SLURM_NODELIST") or None
    step_id = env.get("SLURM_STEP_ID") or env.get("SLURM_STEPID") or None

    if text is None:
        in_allocation = False
        reason = _UNREADABLE_CGROUP
    elif not job_id:
        in_allocation = False
        reason = _NO_JOB_ID
    elif _inside_the_job(components, job_id, bounded):
        in_allocation = True
        reason = ""
    else:
        in_allocation = False
        reason = _ENVIRONMENT_ONLY

    mounts = _mount_table(mountinfo_path or MOUNTINFO_PATH)
    mount_source = f"mount table: {mountinfo_path or MOUNTINFO_PATH}"
    tmp_filesystem = _filesystem_type(tmp_path, mounts)
    home = home_path or env.get("HOME") or "/home"
    home_filesystem = _filesystem_type(home, mounts)
    tmp_is_node_local = _is_node_local(tmp_path, tmp_filesystem)

    return HostFacts(
        in_allocation=in_allocation,
        job_id=job_id,
        step_id=step_id,
        node=node,
        tmp_filesystem=tmp_filesystem,
        home_filesystem=home_filesystem,
        tmp_is_node_local=tmp_is_node_local,
        reason=reason,
        sources={
            "in_allocation": f"{cgroup_source}; {_SOURCE_CGROUP_WALK}",
            "job_id": "environment: SLURM_JOB_ID",
            "step_id": "environment: SLURM_STEP_ID",
            "node": "environment: SLURMD_NODENAME",
            "tmp_filesystem": mount_source,
            "home_filesystem": mount_source,
            "tmp_is_node_local": (
                "reckon.crew.dispatch._is_node_local_path/_path_is_tmpfs, with "
                f"the mount type from {mount_source}"
            ),
        },
    )
