"""A fenced worker's ``/tmp`` is its own scratch directory.

The fence seals the node's ``/tmp`` read-only and grants the run's scratch
directory as its ``TMPDIR``. Workers nevertheless write literal ``/tmp`` paths,
and the standing guidance every job follows exports ``TMPDIR=/tmp``, which
points ``tempfile`` back at the sealed directory. Measured on 14 November
between 09:00 and 10:40Z: at least eight runs failed with ``Read-only file
system`` doing so, and one recorded its negative control failing as
``tempfile.mkdtemp failed on fenced read-only /tmp``.

The fence now mounts the run's own scratch directory over the node tmp, after
sealing it, so every ``/tmp`` path a worker or its tools write lands inside
that scratch whatever ``TMPDIR`` says. The seal stays beneath the node tmp, and
the run's scratch is mounted over it: a sibling run's scratch and every other
session's file under the node tmp read as absent rather than merely read-only,
and only the runtime paths a launch must write — the fleet's sockets and codex's
sandbox marker directory — are re-bound inside the fence's view of the node tmp.

The fixture composes against the node's real ``/tmp``, because the contract is
about that literal path. A temp directory holds the run's scratch, a sibling
run's scratch, another session's file and the two runtime paths, all beneath
the node tmp the fence seals. Where ``/tmp`` is not writable to the test process
(a fenced worker running its own suite) the same structure is built under the
test's temporary directory instead, and the worker is handed that path through
``$FENCE_NODE_TMP`` so the mount is exercised at whichever node tmp this host
would seal.

The property is asserted three ways, because an outline assertion cannot tell a
fence that holds from one spelled correctly and doing nothing, and an executed
refusal cannot tell which overlay produced it:

* The outline is asserted on the *composed argv*: the node tmp is sealed, and
  the run's scratch directory is then bound over it, before any writable grant.
* The fence holds, proved by *executing* the composed argv:
  1. a fenced command writes ``/tmp/probe`` and runs ``tempfile.mkdtemp()``
     with ``$TMPDIR`` exported as ``/tmp``, and both land in the run's scratch;
  2. a fenced command cannot see a sibling run's scratch or a file another
     session created under the node tmp, and neither is harmed by its attempt;
  3. each runtime path the launch writes is reachable, and its write lands in
     the node's real runtime directory.
* A composition whose scratch root sits outside the node tmp mounts nothing,
  so a purely synthetic scratch never invents a ``/tmp`` it does not own.

The declared negative control drops the mount and leaves the seal, exactly as
the control string states: ``worker_tmp_mount_source`` is patched to return
None, so the first executed test's write to ``/tmp/probe`` meets the read-only
seal and fails with ``Read-only file system``. It is applied by running with
``RECKON_TEST_TMP_MOUNT_NEGATIVE=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from reckon import _worker_fence

NEGATIVE_CONTROL_MUTATION = (
    "sealing /tmp without mounting the run's scratch there fails the write with "
    "Read-only file system — patch worker_tmp_mount_source to return None so the "
    "composition emits no mount over the node tmp and the first executed test's "
    "write to /tmp/probe meets the read-only seal"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_TMP_MOUNT_NEGATIVE") == "1"

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

RUN_ID = "r-20261114T120000000000-tmp-owner"
SIBLING_ID = "r-20261114T115500000000-tmp-sibling"

PYTHON = shutil.which("python3") or "python3"

# Stands in for the harness: writes a literal node-tmp path and creates a
# temporary directory through tempfile with ``TMPDIR`` exported as the node tmp,
# recording what landed. Both outcomes go to a results file inside the run
# directory, which the fence always grants, so the stub holds no assumption
# about the boundary. ``$FENCE_NODE_TMP`` is ``/tmp`` on the node the fence
# seals.
_WRITE_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if printf 'ok\\n' > "$FENCE_NODE_TMP/probe" 2>"$FENCE_ERR"; then
  echo "tmp-probe-written" >> "$FENCE_RESULTS"
else
  echo "tmp-probe-refused" >> "$FENCE_RESULTS"
  head -c 400 "$FENCE_ERR" >> "$FENCE_RESULTS"
  exit 1
fi
"$FENCE_PYTHON" "$FENCE_PY" >> "$FENCE_RESULTS" 2>&1 || exit 1
exit 0
"""

_TEMPFILE_PY = """import os
import tempfile

directory = tempfile.mkdtemp()
print("mkdtemp:" + directory)
with open(os.path.join(directory, "leaf"), "w") as handle:
    handle.write("leaf\\n")
"""

# Stands in for a run reaching for what it does not own: it reads and then
# writes a sibling run's scratch and another session's file under the node tmp.
# It never fails, so what the fence allowed is read from its own record.
_REACH_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if [ -e "$FENCE_SIBLING" ]; then
  echo "sibling:visible" >> "$FENCE_RESULTS"
else
  echo "sibling:absent" >> "$FENCE_RESULTS"
fi
rm -rf "$FENCE_SIBLING"
if [ -d "$FENCE_SIBLING" ]; then
  echo "sibling:survived-rm" >> "$FENCE_RESULTS"
else
  echo "sibling:removed" >> "$FENCE_RESULTS"
fi
if [ -e "$FENCE_OUTSIDE" ]; then
  echo "outside:visible" >> "$FENCE_RESULTS"
else
  echo "outside:absent" >> "$FENCE_RESULTS"
fi
if printf 'intruder\\n' > "$FENCE_OUTSIDE" 2>/dev/null; then
  echo "outside:written" >> "$FENCE_RESULTS"
else
  echo "outside:write-refused" >> "$FENCE_RESULTS"
fi
exit 0
"""

# Stands in for the launch's own runtime writes: each runtime path must be
# reachable inside the fence, and the file must land in the node's real runtime
# directory rather than anywhere the fence invented.
_RUNTIME_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
for runtime in $FENCE_RUNTIME_PATHS; do
  if printf 'reachable\\n' > "$runtime/reachable" 2>/dev/null; then
    echo "runtime-ok:$runtime" >> "$FENCE_RESULTS"
  else
    echo "runtime-refused:$runtime" >> "$FENCE_RESULTS"
    exit 1
  fi
done
exit 0
"""


def _node_tmp_root(self_root: Path) -> Path:
    """The node tmp to compose against: ``/tmp`` where the test can write it.

    The contract names the node's real temporary directory, and production pins
    the scratch root beneath it, so the fixture uses ``/tmp`` itself whenever
    this process may create a directory there. A process that cannot — a fenced
    worker running the suite inside its own sandbox — falls back to a synthetic
    directory and hands its path to the worker, so the mount is still exercised
    at a node tmp the fence seals.
    """
    node = Path("/tmp")  # noqa: S108 - the node tmp the fence seals
    if os.access(node, os.W_OK):
        return node
    synthetic = self_root / "node-tmp"
    synthetic.mkdir(parents=True, exist_ok=True)
    return synthetic


class ScratchTree:
    """A node tmp holding the run's scratch, a sibling's, and an outsider's file."""

    def __init__(self, root: Path) -> None:
        self.base = root
        self.node_tmp = _node_tmp_root(root)
        self.fixture_root = Path(
            tempfile.mkdtemp(
                prefix=f"reckon-tmp-fixture-{uuid.uuid4().hex[:8]}-", dir=self.node_tmp
            )
        )
        self.scratch_root = self.fixture_root / "reckon-crew-scratch"
        self.own = self.scratch_root / RUN_ID
        self.sibling = self.scratch_root / SIBLING_ID
        self.own.mkdir(parents=True)
        self.sibling.mkdir(parents=True)
        (self.sibling / "keep.txt").write_text("sibling\n")
        # A file another session created directly in the node tmp, outside the
        # run's scratch and outside the runtime paths.
        self.outside = self.fixture_root / "other-session.txt"
        self.outside.write_text("another session's file\n")
        self.fleet = self.fixture_root / "39486-fleet"
        self.codex = self.fixture_root / ".codex"
        self.fleet.mkdir()
        self.codex.mkdir()
        self.home = root / "home"
        self.run = self.home / ".config" / "reckon" / "crew" / "runs" / RUN_ID
        self.run.mkdir(parents=True)
        self.manifest = self.run / "manifest.md"
        self.results = self.run / "results.txt"
        self.errors = self.run / "stub-errors.txt"
        self.python_script = self.run / "mkdtemp.py"
        self.python_script.write_text(_TEMPFILE_PY)

    def cleanup(self) -> None:
        shutil.rmtree(self.fixture_root, ignore_errors=True)

    def point_the_node_tmp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", self.node_tmp)
        monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(self.scratch_root))
        # The two runtime directories live under the node tmp this fixture owns,
        # so the carve-out is exercised without touching the host's own
        # ``/tmp/.codex`` or a live fleet socket directory.
        monkeypatch.setattr(
            _worker_fence, "node_tmp_runtime_paths", lambda: [self.fleet, self.codex]
        )
        # The host's real delivery stores are not this fixture's business; with
        # them sealed, the expected argv would depend on the machine the test
        # runs on rather than on the fixture.
        monkeypatch.setattr(_worker_fence, "shared_store_seal_roots", list)

    def stub(self, body: str) -> Path:
        path = self.base / "stub.sh"
        path.write_text(body)
        path.chmod(0o755)
        return path

    def argv(self, stub: Path) -> list[str]:
        """Compose the fence the way a dispatch does, through the production path."""
        return _worker_fence.fence_argv(
            [str(stub)],
            writable_directories=[str(self.own)],
            manifest_path=str(self.manifest),
            home=str(self.home),
        )

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            # The standing guidance every job follows, which is exactly what
            # points tempfile back at the node tmp.
            "TMPDIR": str(self.node_tmp),
            "FENCE_NODE_TMP": str(self.node_tmp),
            "FENCE_RESULTS": str(self.results),
            "FENCE_ERR": str(self.errors),
            "FENCE_PYTHON": PYTHON,
            "FENCE_PY": str(self.python_script),
            "FENCE_SIBLING": str(self.sibling),
            "FENCE_OUTSIDE": str(self.outside),
            "FENCE_RUNTIME_PATHS": " ".join(str(p) for p in self.runtime_paths()),
        }
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.base),
            capture_output=True,
            text=True,
            check=False,
        )

    def runtime_paths(self) -> list[Path]:
        return [self.fleet, self.codex]

    def results_text(self) -> str:
        return self.results.read_text()


@pytest.fixture
def tree(tmp_path: Path) -> ScratchTree:
    """A composed-fixture tree that removes its node-tmp scratch afterwards."""
    built = ScratchTree(tmp_path)
    try:
        yield built
    finally:
        built.cleanup()


def _mount_of(argv: list[str], destination: Path) -> int:
    """Return the index of the writable ``--bind`` onto ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part != "--bind" or index + 2 >= len(argv):
            continue
        if argv[index + 2] == target:
            return index
    return -1


def _ro_bind_of(argv: list[str], destination: Path) -> int:
    """Return the index of the read-only overlay of ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == "--ro-bind" and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def test_the_run_scratch_is_mounted_over_the_node_tmp(
    tree: ScratchTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node tmp is sealed, then the run's scratch is bound over it."""
    tree.point_the_node_tmp(monkeypatch)
    argv = tree.argv(tree.stub(_WRITE_STUB))
    seal = _ro_bind_of(argv, tree.node_tmp)
    mount = _mount_of(argv, tree.node_tmp)
    # Positive control: an absent seal or mount would mean the ordering
    # assertions below are testing nothing rather than passing.
    assert seal != -1, "the node tmp was not sealed read-only"
    assert mount != -1, "the run's scratch was not mounted over the node tmp"
    assert mount > seal, (
        "the scratch mount precedes the node-tmp seal, so the seal would shadow "
        "the scratch"
    )
    assert argv[mount + 1] == str(_worker_fence.resolved_destination(tree.own)), (
        "the mount over the node tmp does not name the run's own scratch: "
        f"{argv[mount : mount + 3]}"
    )


def test_no_mount_is_composed_for_a_scratch_root_outside_the_node_tmp(
    tmp_path: Path,
) -> None:
    """A scratch root elsewhere is not reachable through the node tmp at all."""
    node_tmp = tmp_path / "node"
    node_tmp.mkdir()
    outside_root = tmp_path / "elsewhere"
    own = outside_root / RUN_ID
    own.mkdir(parents=True)
    assert _worker_fence.worker_tmp_mount_source([own], node_tmp=node_tmp) is None
    assert _worker_fence.worker_tmp_mount_source([], node_tmp=node_tmp) is None
    assert _worker_fence.worker_tmp_mount_source([own], node_tmp=None) is None


@pytest.fixture
def declared_mount_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared negative control when the env var asks for it.

    The mutation drops the mount exactly as the control string states: no
    scratch directory is returned for the node tmp, so the composition keeps the
    read-only seal and nothing else, and the worker's node-tmp write meets it.
    """
    if not NEGATIVE_CONTROL:
        return
    monkeypatch.setattr(
        _worker_fence, "worker_tmp_mount_source", lambda *args, **kwargs: None
    )


@requires_bwrap
def test_a_write_to_tmp_and_a_tempfile_directory_land_in_the_run_scratch(
    tree: ScratchTree,
    monkeypatch: pytest.MonkeyPatch,
    declared_mount_mutation: None,
) -> None:
    """Case 1: the node-tmp write and tempfile both succeed in the run's scratch."""
    tree.point_the_node_tmp(monkeypatch)
    proc = tree.run_fence(tree.argv(tree.stub(_WRITE_STUB)))
    results = tree.results_text()
    # Positive control: the stub records only from inside the fence, so a record
    # naming the write proves the shim ran and reached the node tmp at all.
    assert "tmp-probe-written" in results, (
        f"the fenced write to {tree.node_tmp}/probe did not land: {results!r}; "
        f"stderr: {proc.stderr!r}"
    )
    assert (tree.own / "probe").read_text() == "ok\n", (
        "the node-tmp write did not land in the run's own scratch"
    )
    made = [
        line.removeprefix("mkdtemp:")
        for line in results.splitlines()
        if line.startswith("mkdtemp:")
    ]
    assert made, f"tempfile.mkdtemp() did not report: {results!r}"
    created = made[-1]
    assert Path(created).parent == Path(str(tree.node_tmp)), (
        f"mkdtemp created {created}, outside the node tmp {tree.node_tmp}"
    )
    landed = tree.own / Path(created).name
    assert landed.is_dir(), (
        f"the directory mkdtemp created at {created} is not in the run's scratch"
    )
    assert (landed / "leaf").read_text() == "leaf\n"
    assert proc.returncode == 0, f"the fenced stub exited {proc.returncode}"


@requires_bwrap
def test_a_sibling_scratch_and_another_sessions_file_are_unreachable(
    tree: ScratchTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 2: neither a sibling's scratch nor an outsider's file is reachable."""
    tree.point_the_node_tmp(monkeypatch)
    proc = tree.run_fence(tree.argv(tree.stub(_REACH_STUB)))
    results = tree.results_text()
    # Positive control: the stub records only from inside the fence, so the
    # record proves it ran rather than that it died before the reads.
    assert results, "the fenced stub recorded nothing"
    assert "sibling:absent" in results, (
        f"a sibling run's scratch was visible inside the fence: {results!r}"
    )
    assert "outside:absent" in results, (
        f"another session's file under the node tmp was visible: {results!r}"
    )
    # The attempt can only be harmless: the paths it names are absent inside the
    # fence, so what reaches them is the run's own scratch rather than the host
    # inode the record below checks.
    assert tree.sibling.is_dir(), "the sibling scratch was removed on the host"
    assert (tree.sibling / "keep.txt").read_text() == "sibling\n"
    assert tree.outside.read_text() == "another session's file\n", (
        "another session's file under the node tmp was overwritten"
    )
    assert proc.returncode == 0, f"the fenced stub exited {proc.returncode}"


@requires_bwrap
def test_the_runtime_paths_stay_reachable(
    tree: ScratchTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 3: each runtime path is writable and maps to its real directory."""
    tree.point_the_node_tmp(monkeypatch)
    proc = tree.run_fence(tree.argv(tree.stub(_RUNTIME_STUB)))
    results = tree.results_text()
    assert results, "the fenced stub recorded nothing"
    for runtime in tree.runtime_paths():
        assert f"runtime-ok:{runtime}" in results, (
            f"the runtime path {runtime} was not writable inside the fence: "
            f"{results!r}; stderr: {proc.stderr!r}"
        )
        assert (runtime / "reachable").read_text() == "reachable\n", (
            f"the write at {runtime} did not land in the node's real runtime directory"
        )
    assert proc.returncode == 0, f"the fenced stub exited {proc.returncode}"
