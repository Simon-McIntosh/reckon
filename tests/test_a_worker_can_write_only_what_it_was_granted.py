"""A worker's fence is read-only everywhere except the paths it was handed.

The fence used to dev-bind ``/`` writable and overlay the paths somebody had
thought to protect. On 19 November a worker of another project ran ``rm -rf``
naming its own cache directory and, by mistake, a shared project root that was
not on the list; the whole root went, together with its launcher, gate file,
credentials, receipts and model weights. A fence that lists what is protected
protects only what somebody thought to list.

This module asserts the inverted contract three ways, because a shape
assertion cannot tell a read-only fence from one spelled correctly and doing
nothing, and an executed refusal cannot tell which mount produced it:

* the *shape* of the composed argv — the root is mounted read-only, and the
  run's own roots are bound writable after it;
* the *behaviour*, proved by executing the argv and reading what the kernel
  did: writes inside the worktree, the run directory, the declared grant and
  the scratch land, while a write and a recursive removal at a sibling
  checkout, the home directory, a shared project root and a path under the
  node's tmp outside the scratch are refused and leave their targets
  unchanged;
* the *refusal* at composition: a declared grant naming a filesystem root or a
  project root raises before any mount is composed, naming the path.

Every path is synthesised under the test's own temporary tree: nothing here
reads or writes a real checkout, the operator's home or the live crew home.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _worker_fence

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

RUN_ID = "r-20261119T090000000000-under-test"

# Stands in for a run's own work. Every path arrives through the environment so
# the stub holds no assumption about where the fence put the boundary, and
# every probe records what the kernel answered rather than what was asked.
_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
probe_write() {
  if touch "$2" 2>/dev/null; then
    echo "$1=wrote" >> "$FENCE_RESULTS"
  else
    echo "$1=refused" >> "$FENCE_RESULTS"
  fi
}
probe_remove() {
  if rm -rf "$2" 2>/dev/null; then
    echo "$1=removed" >> "$FENCE_RESULTS"
  else
    echo "$1=refused" >> "$FENCE_RESULTS"
  fi
}
probe_write worktree "$FENCE_WORKTREE/probe.txt"
probe_write run-directory "$FENCE_RUN/probe.txt"
probe_write scratch "$FENCE_SCRATCH/probe.txt"
probe_write declared-grant "$FENCE_GRANT/probe.txt"
probe_write grant-parent "$FENCE_GRANT_PARENT/probe.txt"
probe_write sibling-checkout "$FENCE_SIBLING/probe.txt"
probe_write home "$FENCE_HOME/secret.txt"
probe_write node-tmp "$FENCE_NODE_TMP/other-session.txt"
probe_remove sibling-checkout-removal "$FENCE_SIBLING"
probe_remove home-removal "$FENCE_HOME"
probe_remove shared-root-removal "$FENCE_SHARED"
probe_remove node-tmp-removal "$FENCE_NODE_TMP/other-session.txt"
echo finished >> "$FENCE_RESULTS"
"""


class World:
    """A temporary world in the shape a dispatch fences a worker into.

    One run's own roots — its worktree, run directory and scratch — a declared
    grant on shared storage with a parent the fence must not open, and the four
    targets the fence must refuse: a sibling checkout, the home directory, a
    shared project root and a path under the node's tmp outside the scratch.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.worktree = root / "worktree"
        self.node_tmp = root / "node-tmp"
        self.scratch = root / "crew-scratch" / RUN_ID
        self.crew_home = root / "crew-home"
        self.run = self.crew_home / "crew" / "runs" / RUN_ID
        self.shared = root / "shared-project"
        self.grant_parent = root / "shared-storage"
        self.grant = self.grant_parent / "granted"
        self.sibling = self.home / "Code" / "another-checkout"
        self.results = self.run / "results.txt"
        self.stub = root / "stub.sh"
        for directory in (
            self.home / ".ssh",
            self.worktree,
            self.scratch,
            self.run,
            self.grant,
            self.node_tmp,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        # The home carries a file the fence must leave untouched, and the node
        # tmp carries another session's file.
        self.home_secret = self.home / "secret.txt"
        self.home_secret.write_text("operator credential\n")
        self.outside_tmp = self.node_tmp / "other-session.txt"
        self.outside_tmp.write_text("another session's file\n")
        # A sibling checkout: a main checkout of another project, which the
        # fence must seal whatever the protected-path list says.
        self.sibling.mkdir(parents=True)
        (self.sibling / ".git").mkdir()
        (self.sibling / "tracked.txt").write_text("another project's work\n")
        # A shared project root: not a checkout the fence names, and the
        # directory whose removal the incident's call performed.
        self.shared.mkdir()
        (self.shared / "weights.bin").write_text("model weights\n")
        (self.shared / "evidence").mkdir()
        (self.shared / "evidence" / "record.html").write_text("<html></html>\n")
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)

    def point_the_fence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point the fence's node-local seams at this world's directories."""
        monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", self.node_tmp)
        monkeypatch.setenv(
            "RECKON_WORKER_SCRATCH_ROOT", str(self.root / "crew-scratch")
        )

    def argv(self) -> list[str]:
        """Compose the fence the product builds for this run's worker."""
        return _worker_fence.fence_argv(
            [str(self.stub)],
            writable_directories=[str(self.run), str(self.scratch), str(self.grant)],
            worktree=str(self.worktree),
            manifest_path=str(self.run / "manifest.md"),
            home=str(self.home),
        )

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_WORKTREE": str(self.worktree),
            "FENCE_RUN": str(self.run),
            "FENCE_SCRATCH": str(self.scratch),
            "FENCE_GRANT": str(self.grant),
            "FENCE_GRANT_PARENT": str(self.grant_parent),
            "FENCE_SIBLING": str(self.sibling),
            "FENCE_HOME": str(self.home),
            "FENCE_NODE_TMP": str(self.node_tmp),
            "FENCE_SHARED": str(self.shared),
        }

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            env=self.environment(),
            cwd=str(self.root),
            capture_output=True,
            text=True,
            check=False,
        )

    def probe(self, name: str) -> str:
        """Return the outcome the stub recorded for one probe."""
        for line in self.results.read_text().splitlines():
            key, _, value = line.partition("=")
            if key == name:
                return value
        raise AssertionError(f"the stub recorded no probe named {name!r}")


def _bind_index(argv: list[str], flag: str, destination: Path) -> int:
    """Return the index of a mount whose destination is ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == flag and index + 2 < len(argv) and argv[index + 2] == target:
            return index
    return -1


def test_the_composed_fence_mounts_the_filesystem_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root is read-only and every handed root is bound writable after it."""
    world = World(tmp_path)
    world.point_the_fence(monkeypatch)
    argv = world.argv()
    assert argv[:7] == [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--unshare-pid",
    ]
    assert "--dev-bind" not in argv
    root_at = argv.index("--ro-bind")
    for handed in (world.worktree, world.run, world.scratch, world.grant):
        index = _bind_index(argv, "--bind", handed)
        assert index > root_at, f"{handed} is not bound writable after the root"
    # A declared grant's parent is not a grant of its own.
    assert _bind_index(argv, "--bind", world.grant_parent) == -1


@requires_bwrap
def test_a_fenced_run_writes_only_where_it_was_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run writes its own roots and is refused everywhere else."""
    world = World(tmp_path)
    world.point_the_fence(monkeypatch)
    completed = world.run_fence(world.argv())
    assert completed.returncode == 0, completed.stderr
    assert world.probe("finished") == ""
    for landed in ("worktree", "run-directory", "scratch", "declared-grant"):
        assert world.probe(landed) == "wrote", f"{landed} was not writable"
    for refused in (
        "grant-parent",
        "sibling-checkout",
        "home",
        "node-tmp",
        "sibling-checkout-removal",
        "home-removal",
        "shared-root-removal",
        "node-tmp-removal",
    ):
        assert world.probe(refused) == "refused", f"{refused} was not refused"
    # Every refused target is still exactly as it was.
    assert world.home_secret.read_text() == "operator credential\n"
    assert world.outside_tmp.read_text() == "another session's file\n"
    assert (world.sibling / "tracked.txt").read_text() == "another project's work\n"
    assert (world.shared / "weights.bin").read_text() == "model weights\n"
    assert (world.shared / "evidence" / "record.html").read_text() == "<html></html>\n"
    assert not (world.grant_parent / "probe.txt").exists()
    assert (world.grant / "probe.txt").exists()


def test_a_grant_on_a_filesystem_root_is_refused_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant naming the filesystem root refuses, naming the path."""
    world = World(tmp_path)
    world.point_the_fence(monkeypatch)
    with pytest.raises(_worker_fence.BackendError) as refused:
        _worker_fence.fence_argv(
            [str(world.stub)],
            writable_directories=["/"],
            worktree=str(world.worktree),
            manifest_path=str(world.run / "manifest.md"),
            home=str(world.home),
        )
    message = str(refused.value)
    assert "filesystem root" in message
    assert str(Path("/")) in message


def test_a_grant_on_a_project_root_is_refused_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant naming a project root refuses, naming the path."""
    world = World(tmp_path)
    world.point_the_fence(monkeypatch)
    with pytest.raises(_worker_fence.BackendError) as refused:
        _worker_fence.fence_argv(
            [str(world.stub)],
            writable_directories=[str(world.sibling)],
            worktree=str(world.worktree),
            manifest_path=str(world.run / "manifest.md"),
            home=str(world.home),
        )
    message = str(refused.value)
    assert "project root" in message
    assert str(world.sibling) in message


@requires_bwrap
def test_the_nineteen_november_call_removes_only_the_granted_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident's two-argument removal, replayed against a temporary tree.

    The worker ran ``rm -rf`` naming its own cache directory and a shared
    project root, with the output discarded. Against a tree laid out the same
    way, the granted cache directory goes and the shared root stays whole, so
    the call that emptied a project's evidence tree removes only what the run
    was granted.
    """
    world = World(tmp_path)
    world.point_the_fence(monkeypatch)
    cache = world.grant / "cache"
    cache.mkdir()
    (cache / "entry.bin").write_text("the run's own cache\n")
    stub = tmp_path / "incident-call.sh"
    stub.write_text(
        "#!/bin/sh\n"
        'rm -rf "$INCIDENT_CACHE" "$INCIDENT_SHARED" >/dev/null 2>&1\n'
        'echo "call=$?" >> "$FENCE_RESULTS"\n'
    )
    stub.chmod(0o755)
    argv = _worker_fence.fence_argv(
        [str(stub)],
        writable_directories=[str(world.run), str(world.scratch), str(world.grant)],
        worktree=str(world.worktree),
        manifest_path=str(world.run / "manifest.md"),
        home=str(world.home),
    )
    environment = {
        **world.environment(),
        "INCIDENT_CACHE": str(cache),
        "INCIDENT_SHARED": str(world.shared),
    }
    completed = subprocess.run(
        argv,
        env=environment,
        cwd=str(world.root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not cache.exists()
    assert (world.shared / "weights.bin").read_text() == "model weights\n"
    assert (world.shared / "evidence" / "record.html").read_text() == "<html></html>\n"
    assert world.home_secret.read_text() == "operator credential\n"


def test_the_command_the_dispatch_composes_is_the_read_only_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch a dispatch builds wraps the backend in the read-only fence.

    ``launch_plan`` is what a dispatch calls, so this asserts the contract on
    that composition rather than only on the fence helper it delegates to: the
    harness command sits behind the separator, and the root mount in front of
    it is the read-only one.
    """
    from reckon import _backends

    world = World(tmp_path)
    world.point_the_fence(monkeypatch)
    plan = _backends.launch_plan(
        backend_name="b",
        backend={"launch": "cli", "command": "claude", "sandbox": "worktree-full"},
        prompt="p",
        worktree=str(world.worktree),
        manifest_path=str(world.run / "manifest.md"),
        writable_directories=[str(world.grant)],
        fence=True,
        fence_home=str(world.home),
    )
    assert plan.argv[:7] == [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--unshare-pid",
    ]
    separator = plan.argv.index("--")
    assert plan.argv[separator + 1] == "claude"
    assert _bind_index(plan.argv, "--bind", world.grant) > 0
