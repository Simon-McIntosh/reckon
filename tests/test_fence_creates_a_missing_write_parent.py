"""A fenced launch creates and binds the roots a declared write path needs.

bubblewrap binds a writable root by *source* path, so a root whose directory
has not been created yet aborts the launch with ``bwrap: Can't find source
path ...`` before the worker starts: exit 1 with no stream, which reads as an
abandoned run rather than as a missing directory. Every root the fence binds is
therefore created before the argv is composed — a declared write path, the run
directory a manifest lands in, and the run's delivery stores alike.

The roots are granted through the same composition a dispatch uses:
``launch_plan`` with a tier and the declared paths the dispatcher passes, never
``fence_argv`` with its writable roots filled in by hand. A tier's own write
roots are not enough under a fence — the fence seals almost everything and
re-binds only what it is handed — so the delivery stores a restricted tier gets
are named for every tier, and the composed argv is compared against the one a
restricted tier's dispatch composes.

A declared path that names a file stays a file: it is granted through its
parent directory, so declaring ``report.md`` can never leave a directory named
``report.md`` where the artifact was meant to land.

The declared mutation removes the root creation, and the manifest case must
then fail; a second control drops the declared paths from the fence roots and
the out-of-repo case must then fail. Both red runs are recorded under the run
directory named in the manifest.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _backends

# ``~/.agents`` is a path the fence seals and no tier grants, so a declared
# write path under it is bound only because the declaration is folded into the
# fence's own roots. That is what makes it a case the second control can break.
SEALED_DELIVERY = (".agents", "deliverable")


class Fence:
    """A synthetic operator home with sealed roots and a live run."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        # ``.config/reckon`` is sealed, so a declared root inside it is re-bound
        # writable by the composed argv.
        self.sealed = self.home / ".config" / "reckon"
        self.reports = self.sealed / "reports"
        self.reviews = self.sealed / "reviews"
        self.reports.mkdir(parents=True)
        self.reviews.mkdir(parents=True)
        # A sealed path no tier grants: the declaration is the only reason it is
        # bound, and dropping the declaration leaves it read-only.
        self.agents = self.home / ".agents"
        self.deliverable = self.agents.joinpath(*SEALED_DELIVERY[1:])
        self.deliverable.mkdir(parents=True)
        (self.home / "Code").mkdir()
        self.worktree = self.home / "Code" / "tree"
        self.worktree.mkdir()
        self.run = self.sealed / "runs" / "r1"
        self.run.mkdir(parents=True)
        self.manifest = self.run / "manifest.md"

    def compose(
        self,
        *,
        declared: tuple[Path, ...] = (),
        sandbox: str = _backends.WORKTREE_FULL,
        manifest: Path | None = None,
    ) -> list[str]:
        """Compose a launch the way a dispatch does: fence roots, then argv.

        The declared paths reach the fence by the same route a dispatcher uses —
        folded into the roots the composition is handed — so a case cannot pass
        by filling ``fence_argv``'s writable roots in by hand.
        """
        manifest = self.manifest if manifest is None else manifest
        roots = _backends.fenced_write_roots(
            {"launch": "cli", "sandbox": sandbox},
            repository=self.worktree,
            run_directory=manifest.parent,
            reports_directory=self.reports,
            review_store_directory=self.reviews,
            manifest_path=manifest,
            declared_write_paths=declared,
            worktree=self.worktree,
        )
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": "true",
                "dialect": "claude",
                "sandbox": sandbox,
            },
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(manifest),
            writable_directories=roots,
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


def _launch(argv: list[str]) -> None:
    """Run a composed argv, proving bubblewrap accepts the mounts it names."""
    if shutil.which(_backends.FENCE_BINARY) is None:
        pytest.skip("bubblewrap is not installed")
    launched = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert launched.returncode == 0, launched.stderr
    assert "Can't find source path" not in launched.stderr


def test_an_out_of_repo_declared_file_is_granted_through_its_directory(
    tmp_path: Path,
) -> None:
    """Case (a): a worktree-full run may write the file it declared outside the repo."""
    fence = Fence(tmp_path)
    declared = fence.deliverable / "out.md"
    assert not declared.exists(), "the fixture must start with the file missing"

    argv = fence.compose(declared=(declared,))

    binds = _writable_binds(argv)
    assert declared.parent in binds, (
        "the declared out-of-repo file has no writable bind covering it"
    )
    assert fence.agents in _read_only_binds(argv), "the sealed root lost its overlay"
    # The declaration is what opens it: no other root covers this path.
    assert fence.deliverable not in _read_only_binds(argv)
    assert not declared.is_dir(), "the declared file was created as a directory"
    _launch(argv)


def test_a_declared_file_under_a_missing_directory_keeps_its_name(
    tmp_path: Path,
) -> None:
    """Case (b): the directory is created and the file is not made a directory."""
    fence = Fence(tmp_path)
    declared = fence.reports / "missing" / "report.md"
    assert not declared.parent.exists(), (
        "the fixture must start with the directory missing"
    )

    argv = fence.compose(declared=(declared,))

    assert declared.parent.is_dir(), "the missing directory was not created"
    assert not declared.exists(), "the declared file was created as a directory"
    assert declared.parent in _writable_binds(argv), (
        "the file's directory is not the writable bind the composition names"
    )


def test_a_manifest_under_a_missing_directory_composes(tmp_path: Path) -> None:
    """Case (c): a manifest named in a directory that does not exist yet."""
    fence = Fence(tmp_path)
    run = fence.home / ".config" / "reckon" / "runs" / "absent"
    manifest = run / "manifest.md"
    assert not run.exists(), "the fixture must start with the run directory missing"

    argv = fence.compose(manifest=manifest)

    assert run.is_dir(), "the manifest's directory was not created"
    _launch(argv)


def test_a_fenced_worktree_full_run_binds_the_restricted_delivery_roots(
    tmp_path: Path,
) -> None:
    """Case (d): the delivery stores are granted whatever the tier."""
    fence = Fence(tmp_path)

    full = set(_writable_binds(fence.compose(sandbox=_backends.WORKTREE_FULL)))
    restricted = set(_writable_binds(fence.compose(sandbox=_backends.READ_ONLY)))

    for root in (fence.reports, fence.reviews):
        assert root in restricted, "a restricted tier lost its delivery root"
        assert root in full, (
            "a fenced worktree-full run cannot write the delivery root a "
            "restricted tier gets"
        )


def test_a_write_path_whose_parent_is_a_regular_file_refuses(tmp_path: Path) -> None:
    fence = Fence(tmp_path)
    blocker = tmp_path / "regular"
    blocker.write_text("not a directory")
    declared = blocker / "nested" / "report.md"

    with pytest.raises(_backends.BackendError) as raised:
        fence.compose(declared=(declared,))

    assert str(declared.resolve()) in str(raised.value)
