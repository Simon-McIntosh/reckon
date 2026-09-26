"""A fenced launch creates the directory a declared write path needs.

bubblewrap binds a declared write root by *source* path, so a root whose
directory has not been created yet aborts the launch with ``bwrap: Can't find
source path ...`` before the worker starts: exit 1 with no stream, which reads
as an abandoned run rather than as a missing directory. Every declared write
path whose parent chain is incomplete is therefore created as a directory
before the fence argv is composed, with no wider ancestor bound in its place.

The write path here sits inside a path the fence seals read-only, because that
is the only place a declared root is bound at all: a root outside every sealed
path is already writable through the composition's ``--dev-bind / /`` and needs
no grant of its own. The reports root exists and is sealed, its ``missing``
child does not, so the fence must create the child and bind exactly the path
that was declared rather than the reports root that happened to exist.

Both the composition and the launched process are asserted. The composed argv
names the bind the launch would use, and running it proves bubblewrap accepts
the mount it describes; the composed argv alone cannot show that the source
path is now findable.

The declared mutation removes the directory creation, and the case that asserts
the declared path exists must then fail while the composed launch is refused
with the missing-source condition. That red run is recorded under the run
directory named in the manifest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import _backends

# The child of the sealed reports root that does not exist yet. It is a path
# under a missing directory rather than a bare directory, so the case exercises
# the parent chain a declared landing path carries.
MISSING_CHILD_SUFFIX = ("reports", "missing", "report.md")


class Fence:
    """A synthetic operator home with a sealed reports root and a live run."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        # ``.config/reckon`` is a path the fence seals, so a declared root
        # inside it is re-bound writable by the composed argv.
        self.sealed = self.home / ".config" / "reckon"
        self.reports = self.sealed / "reports"
        self.reports.mkdir(parents=True)
        (self.home / "Code").mkdir()
        self.worktree = self.home / "Code" / "tree"
        self.worktree.mkdir()
        self.run = self.sealed / "runs" / "r1"
        self.run.mkdir(parents=True)

    @property
    def declared(self) -> Path:
        """The declared write path: a child the reports root does not hold."""
        return self.reports.joinpath(*MISSING_CHILD_SUFFIX)

    def compose(self, declared: Path) -> list[str]:
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": "true",
                "dialect": "claude",
                "sandbox": "worktree-full",
            },
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.run / "manifest.md"),
            writable_directories=[str(declared)],
            fence=True,
            fence_home=str(self.home),
        ).argv


def _writable_binds(argv: list[str]) -> list[Path]:
    """Return every writable mount destination in a composed fence argv."""
    destinations: list[Path] = []
    for index, flag in enumerate(argv):
        if flag == "--bind" and index + 2 < len(argv):
            destinations.append(Path(argv[index + 2]))
    return destinations


def _read_only_binds(argv: list[str]) -> list[Path]:
    """Return every read-only mount destination in a composed fence argv."""
    destinations: list[Path] = []
    for index, flag in enumerate(argv):
        if flag == "--ro-bind" and index + 2 < len(argv):
            destinations.append(Path(argv[index + 2]))
    return destinations


def test_a_declared_write_path_under_a_missing_directory_is_created(
    tmp_path: Path,
) -> None:
    fence = Fence(tmp_path)
    declared = fence.declared
    assert not declared.exists(), "the fixture must start with the path missing"

    argv = fence.compose(declared)

    # The declared path itself is created; the reports root it lives under is
    # sealed read-only, never bound writable in its place.
    assert declared.is_dir(), "the declared write path was not created"
    assert fence.sealed in _read_only_binds(argv), "the sealed root lost its overlay"
    assert fence.reports not in _writable_binds(argv), (
        "the reports root was bound writable, which is wider than declared"
    )
    assert declared in _writable_binds(argv), (
        "the declared path is not the writable bind the composition names"
    )
    # Proves bubblewrap accepts the mount the argv describes: before the
    # creation this is `bwrap: Can't find source path ...` with a non-zero exit.
    launched = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert launched.returncode == 0, launched.stderr
    assert "Can't find source path" not in launched.stderr


def test_a_declared_path_that_already_exists_keeps_its_bind(tmp_path: Path) -> None:
    """An existing declared file is bound by name, not by its directory."""
    fence = Fence(tmp_path)
    declared = fence.reports / "report.md"
    declared.write_text("exists")

    argv = fence.compose(declared)

    assert declared in _writable_binds(argv)
    assert fence.reports not in _writable_binds(argv)


def test_a_write_path_whose_parent_is_a_regular_file_refuses(tmp_path: Path) -> None:
    fence = Fence(tmp_path)
    blocker = tmp_path / "regular"
    blocker.write_text("not a directory")
    declared = blocker / "nested" / "report.md"

    with pytest.raises(_backends.BackendError) as raised:
        fence.compose(declared)

    assert str(declared.resolve()) in str(raised.value)
