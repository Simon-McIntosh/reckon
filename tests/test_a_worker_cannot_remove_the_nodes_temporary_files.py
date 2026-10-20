"""A fenced worker sees the node's /tmp read-only outside its own scratch.

The fence dev-binds ``/`` writable and overlays only the declared protected
paths read-only. The crew scratch root is sealed, but the rest of the node's
``/tmp`` is not, so every fenced worker shares it writable with every other
session on the node under one uid. On 2026-10-18 a run's cleanup named
``rm -rf /tmp/tmp*``: that glob covers the node's shared ``/tmp``, not the
worker's own scratch, and its output was discarded, so what it removed is
unknown. Python's ``tempfile`` default, pytest's ``/tmp/pytest-of-<user>`` and
the fleet's runtime sockets all live there.

The fence closes this by sealing the node's ``/tmp`` read-only among the
protected paths, then re-binding writable only the run's own scratch directory
(its ``TMPDIR``) and the runtime paths its launch must write — the fleet's
sockets and codex's sandbox marker directory, enumerated once in
``node_tmp_runtime_paths`` beside ``worker_scratch_seal_root``.

The property is asserted three ways, because a shape assertion cannot tell a
fence that holds from one spelled correctly and doing nothing, and an executed
refusal cannot tell which overlay produced it:

* The outline is asserted on the *composed argv*: the node tmp is overlaid
  read-only, the run's own scratch and each enumerated runtime path are bound
  writable, and a file outside the scratch and the runtime paths is not.
* The fence holds, proved by *executing* the composed argv: a fenced command
  creates and removes a file in its own scratch and reaches each runtime path,
  while a file it created in the node tmp outside those is refused and survives.
* The runtime directories are *created* before the argv is composed, so a node
  whose harness has never run still gets the grant: composed on a temporary
  node tmp whose runtime directories do not exist, each one is found created
  and bound writable.

The declared negative control strips the node-tmp seal from the fence — the
seam that decides it is patched so no read-only overlay of the node tmp is
emitted — and the outside file is then removed. It is applied by running with
``RECKON_TEST_NODE_TMP_SEAL_NEGATIVE=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _worker_fence

NEGATIVE_CONTROL_MUTATION = (
    "drop the /tmp seal, and the outside file must be removed — patch the "
    "node-tmp seal decision so no read-only overlay of the node tmp is composed; "
    "the fenced rm then deletes the outside file and the test fails"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_NODE_TMP_SEAL_NEGATIVE") == "1"

# The second declared negative control: restore the separate creation loop, so
# ``create_node_tmp_runtime_paths`` creates its directories itself and never
# routes through :func:`reckon._worker_fence.create_write_roots`, and the
# interception test finds no call to it.
RUNTIME_PATHS_NEGATIVE_MUTATION = (
    "restore the separate creation loop in create_node_tmp_runtime_paths, so it "
    "creates its directories itself and never calls create_write_roots; the "
    "interception test then fails because create_write_roots was never called"
)
RUNTIME_PATHS_NEGATIVE = os.environ.get("RECKON_TEST_RUNTIME_PATHS_NEGATIVE") == "1"

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

RUN_ID = "r-20261018T120000000000-under-test"

# Stands in for the harness: writes and deletes a file in its own scratch,
# then in each runtime path, records what landed, and finally removes a file in
# the node tmp outside both, so the command's exit code is the refusal's. Every
# path arrives through the environment, so the stub holds no assumption about
# where the fence put the boundary.
_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if touch "$FENCE_OWN/probe" 2>/dev/null && rm -f "$FENCE_OWN/probe" 2>/dev/null; then
  echo "own-write-delete-ok" >> "$FENCE_RESULTS"
else
  echo "own-write-delete-failed" >> "$FENCE_RESULTS"
fi
for runtime in $FENCE_RUNTIME_PATHS; do
  if touch "$runtime/probe" 2>/dev/null && rm -f "$runtime/probe" 2>/dev/null; then
    echo "runtime-write-delete-ok:$runtime" >> "$FENCE_RESULTS"
  else
    echo "runtime-write-delete-failed:$runtime" >> "$FENCE_RESULTS"
  fi
done
rm -f "$FENCE_OUTSIDE"
"""

# Stands in for a run's stray write: it puts a probe directly into the node tmp
# root, outside its own scratch and the runtime paths the fence carves out, and
# records whether that write was allowed. It writes nowhere else, so the probe
# check is the first and only thing the run does — nothing stands between the
# seal and the assertion.
_PROBE_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if touch "$FENCE_NODE_TMP_PROBE" 2>/dev/null; then
  echo "node-tmp-probe-write-ok" >> "$FENCE_RESULTS"
  exit 0
fi
echo "node-tmp-probe-write-refused" >> "$FENCE_RESULTS"
touch "$FENCE_NODE_TMP_PROBE"
"""


class NodeTmp:
    """A synthetic node tmp: the run's owner scratch, runtime paths, an outsider."""

    def __init__(self, root: Path, *, runtime_paths_exist: bool = True) -> None:
        self.base = root
        self.node_tmp = root / "node-tmp"
        self.scratch_root = self.node_tmp / "reckon-crew-scratch"
        self.own = self.scratch_root / RUN_ID
        self.own.mkdir(parents=True)
        # A file directly in the node tmp, outside the scratch and the runtime
        # paths: the file the fence must refuse to let the worker remove.
        self.outside = self.node_tmp / "tmp-outside.txt"
        self.outside.write_text("other session's file\n")
        # A path a direct-probe stub writes straight into the node tmp root,
        # outside its own scratch and the runtime paths. It must never come into
        # existence, whatever else the fence composes.
        self.node_tmp_probe = self.node_tmp / "probe-direct.txt"
        self.fleet = self.node_tmp / "39486-fleet"
        self.codex = self.node_tmp / ".codex"
        # A node where the harness has never run: codex creates its sandbox
        # marker directory lazily, so on a fresh node it is not there when the
        # fence is composed and the launch is expected to create it.
        if runtime_paths_exist:
            self.fleet.mkdir()
            self.codex.mkdir()
        self.home = root / "home"
        # The protected path the synthetic home carries on disk, so a composed
        # fence seals it read-only and re-binds the run directory beneath it.
        self.rec = self.home / ".config" / "reckon"
        self.run = self.rec / "crew" / "runs" / RUN_ID
        self.run.mkdir(parents=True)
        self.manifest = self.run / "manifest.md"
        self.results = self.run / "results.txt"
        self.stub = root / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)

    def point_the_node_tmp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", self.node_tmp)
        monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(self.scratch_root))
        monkeypatch.setenv("FLEET_RUNTIME_DIR", str(self.fleet))

    def runtime_paths(self) -> list[Path]:
        return [self.fleet, self.codex]

    def argv(self) -> list[str]:
        """Compose the fence the way a dispatch does, through the production path."""
        return _worker_fence.fence_argv(
            [str(self.stub)],
            writable_directories=[str(self.own)],
            manifest_path=str(self.manifest),
            home=str(self.home),
        )

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_OWN": str(self.own),
            "FENCE_OUTSIDE": str(self.outside),
            "FENCE_NODE_TMP_PROBE": str(self.node_tmp_probe),
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

    def results_text(self) -> str:
        return self.results.read_text()


def _bind_of(argv: list[str], destination: Path) -> int:
    """Return the index of the writable ``--bind`` of ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == "--bind" and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def _ro_bind_of(argv: list[str], destination: Path) -> int:
    """Return the index of the read-only overlay of ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == "--ro-bind" and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def test_the_node_tmp_is_sealed_and_the_runtime_paths_are_granted_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node tmp is overlaid read-only; scratch and runtime paths follow."""
    node = NodeTmp(tmp_path)
    node.point_the_node_tmp(monkeypatch)
    argv = node.argv()
    tmp_overlay = _ro_bind_of(argv, node.node_tmp)
    own_bind = _bind_of(argv, node.own)
    # Positive control: the node tmp is actually overlaid, so an absent overlay
    # would mean the assertions below are testing nothing rather than passing.
    assert tmp_overlay != -1, "the node tmp was not overlaid read-only"
    assert own_bind != -1, "the run's own scratch was not bound writable"
    assert own_bind > tmp_overlay, (
        "the own-scratch grant precedes the node-tmp overlay, so the read-only "
        "bind would shadow it"
    )
    for runtime in node.runtime_paths():
        bind = _bind_of(argv, runtime)
        assert bind != -1, f"the runtime path {runtime} was not bound writable"
        assert bind > tmp_overlay, (
            f"the grant of {runtime} precedes the node-tmp overlay, so the "
            "read-only bind would shadow it"
        )
    # No writable grant reaches the outside file's directory beyond the scratch
    # and the runtime paths: the node tmp itself is never granted writable.
    assert _bind_of(argv, node.node_tmp) == -1, "the node tmp itself was granted"


def test_the_runtime_paths_are_enumerated_beside_the_scratch_seal() -> None:
    """The enumerated set is the fleet sockets and codex's sandbox marker dir."""
    # The function reads FLEET_RUNTIME_DIR and NODE_TMP_ROOT at call time; the
    # defaults name the fleet's node-local sockets and codex's registry root.
    paths = _worker_fence.node_tmp_runtime_paths()
    assert len(paths) >= 2, f"expected at least two runtime paths, got {paths}"
    assert all(isinstance(path, Path) for path in paths)


def test_a_runtime_directory_the_node_lacks_is_created_and_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime directory that is not there is created, and is bound writable.

    A grant is composed from a runtime path only while the path exists, so a
    directory the launch creates for itself — codex's sandbox marker directory,
    created lazily on the first sandboxed launch — composes no grant on a node
    where it has not been created yet, and that lane then writes against the
    read-only overlay of ``/tmp``. The composition creates each enumerated
    runtime directory before it builds the argv, so the grant is present on a
    node the harness has never run on. Filtering the enumerated set on existence
    without creating would fail both assertions here.
    """
    node = NodeTmp(tmp_path, runtime_paths_exist=False)
    node.point_the_node_tmp(monkeypatch)
    for runtime in node.runtime_paths():
        assert not runtime.exists(), f"{runtime} existed before the fence"
    argv = node.argv()
    tmp_overlay = _ro_bind_of(argv, node.node_tmp)
    # Positive control: without the overlay there is nothing to grant beneath,
    # so the assertions below would be about a fence this test never built.
    assert tmp_overlay != -1, "the node tmp was not overlaid read-only"
    for runtime in node.runtime_paths():
        assert runtime.is_dir(), (
            f"the runtime directory {runtime} was not created before the argv "
            "was composed, so its grant is absent"
        )
        bind = _bind_of(argv, runtime)
        assert bind != -1, f"the runtime path {runtime} was not bound writable"
        assert bind > tmp_overlay, (
            f"the grant of {runtime} precedes the node-tmp overlay, so the "
            "read-only bind would shadow it"
        )


def test_a_runtime_path_that_is_not_a_directory_is_left_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket or file already at a runtime path is never replaced by a dir."""
    node = NodeTmp(tmp_path, runtime_paths_exist=False)
    node.point_the_node_tmp(monkeypatch)
    # Something other than the launch put a plain file at the fence's runtime
    # path; the composition leaves it as it stands rather than refusing over it.
    node.codex.write_text("not a directory\n")
    argv = node.argv()
    assert node.codex.is_file(), "the fence replaced an existing path with a dir"
    assert node.codex.read_text() == "not a directory\n"
    assert _bind_of(argv, node.codex) != -1, "the composition dropped the path"


@pytest.fixture
def declared_seal_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared negative control when the env var asks for it.

    The mutation removes the node tmp from the sealed set exactly as the control
    string states: the seam that decides the node-tmp seal returns None, so
    :func:`fence_argv` composes no read-only overlay of the node tmp and the
    outside file becomes writable again, as it was before the seal.
    """
    if not NEGATIVE_CONTROL:
        return

    def no_seal() -> None:
        return None

    monkeypatch.setattr(_worker_fence, "node_tmp_seal_root", no_seal)


@requires_bwrap
def test_a_fenced_run_cannot_remove_a_file_outside_its_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_seal_mutation: None,
) -> None:
    """Case 1: the outside removal is refused read-only; scratch and runtime land."""
    node = NodeTmp(tmp_path)
    node.point_the_node_tmp(monkeypatch)
    proc = node.run_fence(node.argv())
    results = node.results_text()
    # Positive control: the stub only writes its record from inside the fence, so
    # a non-empty record proves the shim ran and the runtime paths were reachable.
    assert "own-write-delete-ok" in results, results
    assert not (node.own / "probe").exists()
    for runtime in node.runtime_paths():
        assert f"runtime-write-delete-ok:{runtime}" in results, results
    assert proc.returncode != 0, (
        f"the fenced rm of the outside file exited {proc.returncode}, not "
        f"non-zero; stderr: {proc.stderr!r}"
    )
    # The refusal names the path the worker tried to write, so a reader is told
    # which /tmp path the seal withheld rather than a bare failure.
    assert str(node.outside) in proc.stderr, (
        f"the refusal did not name {node.outside}: {proc.stderr!r}"
    )
    assert "Read-only file system" in proc.stderr, proc.stderr
    assert node.outside.exists(), "the outside file created in the node tmp was removed"
    assert not (node.outside.parent / "probe").exists()


@requires_bwrap
def test_a_probe_written_directly_into_the_node_tmp_root_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_seal_mutation: None,
) -> None:
    """A probe straight into the node tmp root is refused and never lands.

    The removal case proves the seal against a file that already exists; this
    one proves it against a *write*, which is the operation a stray cleanup
    actually performs. The probe is the first assertion in the test — nothing
    stands between the seal and this check — so with the seal removed the
    write succeeds and the test fails here rather than on a later assertion
    about a file some other step placed. The stub writes only the probe, so the
    record in its results file is the write's own outcome.
    """
    node = NodeTmp(tmp_path)
    node.point_the_node_tmp(monkeypatch)
    node.stub.write_text(_PROBE_STUB)
    proc = node.run_fence(node.argv())
    results = node.results_text()
    # Positive control: the stub records only from inside the fence, so a record
    # naming the refusal proves the shim ran and reached the node tmp root.
    assert "node-tmp-probe-write-refused" in results, (
        f"the probe write into the node tmp root was not refused: {results!r}"
    )
    assert not node.node_tmp_probe.exists(), (
        f"a probe landed directly in the node tmp root at {node.node_tmp_probe}"
    )
    assert proc.returncode != 0, (
        f"the fenced probe write exited {proc.returncode}, not non-zero; "
        f"stderr: {proc.stderr!r}"
    )
    assert "Read-only file system" in proc.stderr, proc.stderr


def _separate_creation_loop() -> list[Path]:
    """The pre-routing creation loop, kept only for the declared mutation.

    This is the shape ``create_node_tmp_runtime_paths`` had before it routed
    through :func:`reckon._worker_fence.create_write_roots`: its own resolved-
    destination existence check and ``mkdir`` per path. The second negative
    control installs it so the interception test finds no call to the shared
    creation rule.
    """
    created: list[Path] = []
    for path in _worker_fence.node_tmp_runtime_paths():
        target = _worker_fence.resolved_destination(path)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        created.append(target)
    return created


@pytest.fixture
def declared_runtime_creation_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply the second negative control when the env var asks for it.

    The mutation restores the separate creation loop exactly as the control
    string states: ``create_node_tmp_runtime_paths`` is replaced by the loop
    that creates its directories itself, so it never calls
    :func:`reckon._worker_fence.create_write_roots` and the interception test's
    ``assert seen`` fails.
    """
    if not RUNTIME_PATHS_NEGATIVE:
        return
    monkeypatch.setattr(
        _worker_fence, "create_node_tmp_runtime_paths", _separate_creation_loop
    )


def test_creating_the_runtime_paths_goes_through_create_write_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_runtime_creation_mutation: None,
) -> None:
    """Every runtime path is created by the module's one creation rule.

    ``create_node_tmp_runtime_paths`` once re-implemented
    :func:`reckon._worker_fence.create_write_roots` — the same existence check,
    ``mkdir`` and refusal — so the rule had two copies that could drift. It now
    routes through the shared rule, and this test pins that by intercepting
    ``create_write_roots`` and asserting every runtime path is among the roots
    it hands over.
    """
    node = NodeTmp(tmp_path, runtime_paths_exist=False)
    node.point_the_node_tmp(monkeypatch)
    seen: list[list[Path]] = []
    real_create = _worker_fence.create_write_roots

    def spy(roots) -> None:
        seen.append([Path(root) for root in roots])
        real_create(roots)

    monkeypatch.setattr(_worker_fence, "create_write_roots", spy)
    created = _worker_fence.create_node_tmp_runtime_paths()
    assert seen, "create_node_tmp_runtime_paths did not call create_write_roots"
    roots = seen[-1]
    for runtime in node.runtime_paths():
        assert runtime in roots, (
            f"{runtime} was not among the roots handed to create_write_roots: {roots}"
        )
        assert runtime.is_dir(), f"the runtime path {runtime} was not created"
    assert sorted(created) == sorted(
        _worker_fence.resolved_destination(p) for p in roots
    )


def test_the_composed_fence_argv_for_a_fixed_root_set_is_literal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composed argv for the fixture's fixed roots is exactly this list.

    A downstream shape assertion cannot tell a fence that holds from one spelled
    correctly and doing nothing, so the whole argv for a fixed set of roots is
    pinned here: an added, dropped or reordered mount changes this list. The
    host's two shared delivery stores are pointed away from the machine's real
    ones, so the fixture — not the environment the test runs in — determines the
    expected argv.
    """
    node = NodeTmp(tmp_path)
    node.point_the_node_tmp(monkeypatch)
    monkeypatch.setattr(_worker_fence, "shared_store_seal_roots", list)
    resolve = _worker_fence.resolved_destination
    argv = node.argv()
    expected = [
        _worker_fence.FENCE_BINARY,
        "--dev-bind",
        "/",
        "/",
        # The one protected path the synthetic home holds on disk, sealed
        # read-only before the node tmp seal.
        "--ro-bind",
        str(resolve(node.rec)),
        str(resolve(node.rec)),
        # The run-scratch root, sealed read-only by the scratch seal.
        "--ro-bind",
        str(resolve(node.scratch_root)),
        str(resolve(node.scratch_root)),
        # The node tmp itself, sealed read-only.
        "--ro-bind",
        str(resolve(node.node_tmp)),
        str(resolve(node.node_tmp)),
        # Then the writable grants, in root order: the run's own scratch, the
        # run directory the manifest lives in, then the two runtime paths.
        "--bind",
        str(resolve(node.own)),
        str(resolve(node.own)),
        "--bind",
        str(resolve(node.run)),
        str(resolve(node.run)),
        "--bind",
        str(resolve(node.fleet)),
        str(resolve(node.fleet)),
        "--bind",
        str(resolve(node.codex)),
        str(resolve(node.codex)),
        "--",
        str(node.stub),
    ]
    assert argv == expected
