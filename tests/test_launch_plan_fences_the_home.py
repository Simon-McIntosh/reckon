"""A worker's launch argv seals the operator's home, proved by running it.

The fence is asserted by *executing* the argument vector ``launch_plan``
composes, never by inspecting its shape. A shape assertion cannot tell a fence
that holds from one that is spelled correctly and does nothing, and the whole
point of this change is a boundary an operating system enforces rather than a
rule a worker is asked to obey.

Every protected class gets a temp stand-in under a synthetic home, because the
fence resolves its protected set against an injectable home, so the operator's
real dot directories are never touched. A stub stands in for the harness: it
receives the fence's argv, attempts an ``rm -rf`` of a sentinel in each
protected stand-in and a write into each granted root, and records what
happened in the run directory. The test reads that record back.

Two properties, and the second is what makes the first mean anything:

* the fence holds: every protected sentinel survives, every granted root is
  writable, and the results file proves the run directory was writable because
  the stub could not have written it otherwise;
* the declared negative control drops the ``--ro-bind`` for the ``~/.claude``
  stand-in, and that stand-in's sentinel is then deleted. Without it the suite
  would pass against a fence that was never applied at all.

Running this file directly reproduces the red log: its first line is the
declared mutation, verbatim, and what follows is the observed deletion.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends

NEGATIVE_CONTROL_MUTATION = (
    "drop the --ro-bind for the ~/.claude stand-in so its sentinel can be deleted"
)

# The second declared mutation: a symlinked protected path bound at its own
# path rather than at its resolved target, which bubblewrap refuses.
SYMLINK_NEGATIVE_CONTROL_MUTATION = (
    "remove the symlink resolution so a symlinked protected path is bound at "
    "its own path; the stub launch must exit nonzero"
)

# The third declared mutation: resolution restored to the leaf alone, so a
# protected path below a symlinked directory is bound at its unresolved path.
ANCESTOR_LEAF_ONLY_MUTATION = (
    "restore leaf-only resolution so a protected path below a symlinked "
    "directory is bound at its unresolved path; the stub launch must exit nonzero"
)

# The fourth: the skip that drops a redundant overlay of a path an
# already-sealed directory covers, removed.
REDUNDANT_OVERLAY_MUTATION = (
    "remove the inside-a-protected-path skip so a path an already-sealed "
    "directory covers is bound a second time; the redundant-overlay check must "
    "catch it"
)

# One entry per protected class the plan names, plus a checkout stand-in.
_NAMED_PROTECTED = (
    ".claude",
    ".claude.json",
    ".codex",
    ".config/reckon",
    ".agents",
    "Code/dotfiles",
    ".ssh",
    ".gitconfig",
    ".config/git",
    ".config/gh",
    ".netrc",
    "public",
    ".local/bin",
)

# A checkout under Code that is not named directly, so the class "every main
# checkout under Code" is exercised by the expansion rather than by a literal.
_CHECKOUT = "Code/zzcheckout"

_STUB = """#!/bin/sh
# Stands in for the harness: exercises the fence and records the outcome.
set -u
: > "$FENCE_RESULTS"
while IFS= read -r p; do
  [ -n "$p" ] || continue
  if err=$(rm -rf "$p" 2>&1); then
    echo "protected-sentinel-deleted $p" >> "$FENCE_RESULTS"
  else
    echo "protected-rm-refused $p :: $err" >> "$FENCE_RESULTS"
  fi
done < "$FENCE_PROTECTED_FILE"
while IFS= read -r g; do
  [ -n "$g" ] || continue
  if touch "$g/.fence-write-probe" 2>/dev/null; then
    echo "grant-write-ok $g" >> "$FENCE_RESULTS"
  else
    echo "grant-write-refused $g" >> "$FENCE_RESULTS"
  fi
done < "$FENCE_GRANTS_FILE"
"""

# Stands in for the harness where a protected path is a symlink. The write
# probes go *through* the link, so they land on the link's target: that is what
# the fence seals, and unlinking the link itself (a write in the unlocked home
# directory) is not the property under test.
_SYMLINK_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
while IFS= read -r p; do
  [ -n "$p" ] || continue
  if echo probe > "$p" 2>/dev/null; then
    echo "symlink-write-landed $p" >> "$FENCE_RESULTS"
  else
    echo "symlink-write-refused $p" >> "$FENCE_RESULTS"
  fi
done < "$FENCE_SYMLINK_FILE"
while IFS= read -r g; do
  [ -n "$g" ] || continue
  if touch "$g/.fence-write-probe" 2>/dev/null; then
    echo "grant-write-ok $g" >> "$FENCE_RESULTS"
  else
    echo "grant-write-refused $g" >> "$FENCE_RESULTS"
  fi
done < "$FENCE_GRANTS_FILE"
"""

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)


class Fence:
    """A synthetic home, its protected stand-ins and the run's own roots."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True)
        self.protected: dict[str, Path] = {}
        for name in (*_NAMED_PROTECTED, _CHECKOUT):
            directory = self.home / name
            directory.mkdir(parents=True)
            (directory / "sentinel").write_text("keep")
            self.protected[name] = directory
        # The checkout class is recognised by a ``.git`` entry, not by name.
        (self.home / _CHECKOUT / ".git").mkdir()
        # A file under a protected root but outside every granted root: the
        # fence must seal it even though part of the same root is writable.
        self.config = self.home / ".config" / "reckon"
        self.config.mkdir(parents=True, exist_ok=True)
        self.sealed = self.config / "flight.toml"
        self.sealed.write_text("keep")
        self.run = self.config / "crew" / "runs" / "r1"
        self.reports = self.config / "crew" / "reports"
        self.run.mkdir(parents=True)
        self.reports.mkdir(parents=True)
        # A worktree inside a protected path, so its writability rests on the
        # fence's re-bind rather than on it simply lying outside the set.
        self.worktree = self.home / "Code" / "dotfiles" / ".worktrees" / "wt"
        self.worktree.mkdir(parents=True)
        self.stub = root / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)
        self.results = self.run / "results.txt"
        self.protected_file = self.run / "protected.txt"
        self.grants_file = self.run / "grants.txt"

    def sentinels(self) -> list[Path]:
        # The per-class sentinels plus the sealed file, so an rm of the sealed
        # file is actually attempted rather than merely observed to survive.
        return [d / "sentinel" for d in self.protected.values()] + [self.sealed]

    def grants(self) -> list[Path]:
        return [self.run, self.reports, self.worktree]

    def argv(self) -> list[str]:
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": str(self.stub),
                "dialect": "claude",
                "sandbox": "worktree-full",
            },
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.run / "manifest.md"),
            writable_directories=[str(self.run), str(self.reports)],
            fence=True,
            fence_home=str(self.home),
        ).argv

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.protected_file.write_text(
            "\n".join(str(p) for p in self.sentinels()) + "\n"
        )
        self.grants_file.write_text("\n".join(str(g) for g in self.grants()) + "\n")
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_PROTECTED_FILE": str(self.protected_file),
            "FENCE_GRANTS_FILE": str(self.grants_file),
        }
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.worktree),
            capture_output=True,
            text=True,
            check=False,
        )

    def results_text(self) -> str:
        return self.results.read_text()


class SymlinkFence(Fence):
    """A home in which two protected paths are symlinks.

    One resolves into another protected directory, so the fence emits no bind of
    its own for it: the directory the link lands in is sealed already. The other
    resolves to a file outside every protected path, so the fence resolves it
    and binds that target read-only, because the link's own path cannot be a
    mount destination. This is the shape the operator's own home has, where
    ``~/.gitconfig`` is a symlink into ``Code/dotfiles``.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        # A protected path that is a symlink into another protected directory.
        self.inside_target = self.home / "Code" / "dotfiles" / "git" / "gitconfig"
        self.inside_target.parent.mkdir(parents=True, exist_ok=True)
        self.inside_target.write_text("keep")
        self._repoint(".gitconfig", self.inside_target)
        # A protected path that is a symlink to a file outside every protected
        # path, so the link's target needs an overlay of its own.
        self.outside_dir = self.home / "outside"
        self.outside_dir.mkdir()
        self.outside_target = self.outside_dir / "netrc"
        self.outside_target.write_text("keep")
        self._repoint(".netrc", self.outside_target)
        self.symlinks_file = self.run / "symlinks.txt"
        self.stub.write_text(_SYMLINK_STUB)

    def _repoint(self, name: str, target: Path) -> None:
        link = self.home / name
        if link.is_dir():
            shutil.rmtree(link)
        elif link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        self.protected[name] = link

    def symlinks(self) -> list[Path]:
        return [self.protected[".gitconfig"], self.protected[".netrc"]]

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.symlinks_file.write_text("\n".join(str(p) for p in self.symlinks()) + "\n")
        self.grants_file.write_text("\n".join(str(g) for g in self.grants()) + "\n")
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_SYMLINK_FILE": str(self.symlinks_file),
            "FENCE_GRANTS_FILE": str(self.grants_file),
        }
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.worktree),
            capture_output=True,
            text=True,
            check=False,
        )


class AncestorSymlinkFence(Fence):
    """A home whose ``.config`` is a symlink, so a protected child has a link above it.

    The leaf is an ordinary directory here, which is the shape the first repair
    missed: the resolution triggered on the leaf's own ``is_symlink()``, so
    ``~/.config/reckon`` was bound at its own path and bubblewrap refused it
    with ``Can't mkdir`` — a home with ``~/.config`` symlinked aborts every
    fenced launch. The run's own write root also lives below the link, so the
    same home proves the writable grants are resolved with the overlays.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        real = self.home / "realconfig"
        shutil.move(str(self.home / ".config"), str(real))
        (self.home / ".config").symlink_to(real)
        self.real_config = real

    def linked_protected(self) -> list[Path]:
        """The protected paths reached through the symlinked directory."""
        return [
            self.protected[".config/reckon"],
            self.protected[".config/git"],
            self.protected[".config/gh"],
        ]


def _drop_ro_bind(argv: list[str], path: Path) -> list[str]:
    """Return argv with the read-only overlay of ``path`` removed.

    The declared mutation: the rest of the fence stays, so a change that
    deletes the sentinel proves the removed overlay was the thing holding it.
    """
    target = str(path)
    kept: list[str] = []
    index = 0
    while index < len(argv):
        if (
            argv[index] == "--ro-bind"
            and index + 2 < len(argv)
            and argv[index + 1] == target
            and argv[index + 2] == target
        ):
            index += 3
            continue
        kept.append(argv[index])
        index += 1
    return kept


def _bind_destinations(argv: list[str]) -> list[Path]:
    """Return every mount destination in a composed fence argv."""
    destinations: list[Path] = []
    for index, flag in enumerate(argv):
        if flag in ("--ro-bind", "--bind", "--dev-bind") and index + 2 < len(argv):
            destinations.append(Path(argv[index + 2]))
    return destinations


def _has_symlink_component(path: Path) -> bool:
    """True if the path itself or any ancestor is a symlink.

    bubblewrap refuses to create a mount point: nowhere below a link, so a
    destination is usable only when no component of it is one — a leaf that is
    an ordinary directory with a symlinked parent aborts the launch exactly as
    a symlinked leaf does.
    """
    return any(Path(part).is_symlink() for part in (path, *path.parents))


def _overlays_inside_another(argv: list[str]) -> list[Path]:
    """Return read-only destinations an already-sealed directory covers.

    The observable of the clause that drops a redundant overlay: a second
    read-only bind of a path inside a directory that is sealed in its own right
    is accepted by bubblewrap, so it cannot be caught by executing the argv, but
    it is still argv the composition must not produce.
    """
    destinations = [
        Path(argv[index + 2]) for index, flag in enumerate(argv) if flag == "--ro-bind"
    ]
    return [
        destination
        for destination in destinations
        if any(
            destination != other and destination.is_relative_to(other)
            for other in destinations
        )
    ]


def _bind_again(argv: list[str], target: Path) -> list[str]:
    """Return argv with a second read-only overlay of ``target``.

    The declared mutation for the inside-a-protected-path clause: the skip is
    removed, so a path an already-sealed directory covers is bound a second
    time.
    """
    marker = argv.index("--")
    return [*argv[:marker], "--ro-bind", str(target), str(target), *argv[marker:]]


def _bind_at_the_unresolved_path(
    argv: list[str], unresolved: Path, target: Path
) -> list[str]:
    """Return argv with the resolved overlay replaced by one at its own path.

    The declared mutation: the resolution is undone, so the read-only overlay
    of the resolved target is emitted at the path as the fence spelled it
    instead — which is what bubblewrap refuses to mount when a link sits at the
    path or anywhere above it.
    """
    kept: list[str] = []
    index = 0
    while index < len(argv):
        if (
            argv[index] == "--ro-bind"
            and index + 2 < len(argv)
            and argv[index + 1] == str(target)
            and argv[index + 2] == str(target)
        ):
            kept += ["--ro-bind", str(unresolved), str(unresolved)]
            index += 3
            continue
        kept.append(argv[index])
        index += 1
    return kept


def _negative_control_report(root: Path) -> list[str]:
    fence = Fence(root)
    argv = _drop_ro_bind(fence.argv(), fence.protected[".claude"])
    proc = fence.run_fence(argv)
    sentinel = fence.protected[".claude"] / "sentinel"
    return [
        f"fence argv head: {' '.join(argv[:4])}",
        f"stub exit: {proc.returncode}",
        f"~/.claude sentinel exists after the run: {sentinel.exists()}",
        f"results: {fence.results_text().strip()!r}",
    ]


def _symlink_negative_control_report(root: Path) -> list[str]:
    fence = SymlinkFence(root)
    argv = _bind_at_the_unresolved_path(
        fence.argv(), fence.protected[".netrc"], fence.outside_target
    )
    proc = fence.run_fence(argv)
    return [
        f"fence argv head: {' '.join(argv[:4])}",
        f"stub exit: {proc.returncode}",
        f"stub stderr: {proc.stderr.strip()}",
        f"the stub ran: {fence.results.exists()}",
    ]


def _ancestor_negative_control_report(root: Path) -> list[str]:
    fence = AncestorSymlinkFence(root)
    link = fence.protected[".config/reckon"]
    argv = _bind_at_the_unresolved_path(
        fence.argv(), link, _backends.resolved_destination(link)
    )
    proc = fence.run_fence(argv)
    return [
        f"fence argv head: {' '.join(argv[:4])}",
        f"protected path below the symlinked directory: {link}",
        f"bound at: {link} (unresolved)",
        f"stub exit: {proc.returncode}",
        f"stub stderr: {proc.stderr.strip()}",
        f"the stub ran: {fence.results.exists()}",
    ]


def _redundant_overlay_control_report(root: Path) -> list[str]:
    fence = SymlinkFence(root)
    argv = fence.argv()
    target = _backends.resolved_destination(fence.inside_target)
    mutated = _bind_again(argv, target)
    return [
        f"fence argv head: {' '.join(argv[:4])}",
        f"coverage before: {_overlays_inside_another(argv)}",
        f"redundant target: {target}",
        f"coverage after: {_overlays_inside_another(mutated)}",
    ]


@requires_bwrap
def test_the_fence_seals_every_protected_class(tmp_path: Path) -> None:
    fence = Fence(tmp_path)
    argv = fence.argv()
    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    assert "--ro-bind" in argv

    proc = fence.run_fence(argv)
    assert proc.returncode == 0, proc.stderr
    results = fence.results_text()

    # Positive control: the stub only writes results from inside the fence, so
    # a non-empty record proves the run directory was writable and the stub ran.
    assert "protected-rm-refused" in results

    for path in fence.sentinels():
        assert path.exists(), f"protected sentinel did not survive: {path}"
        assert f"protected-rm-refused {path}" in results, path

    for grant in fence.grants():
        assert f"grant-write-ok {grant}" in results, grant
        assert (grant / ".fence-write-probe").exists(), grant


@requires_bwrap
def test_the_negative_control_deletes_the_claude_sentinel(tmp_path: Path) -> None:
    """The declared mutation: without the ``~/.claude`` overlay it dies.

    If the wrong path were dropped the sentinel would survive and this test
    would fail, so the deletion is attributable to the removed overlay.
    """
    fence = Fence(tmp_path)
    argv = _drop_ro_bind(fence.argv(), fence.protected[".claude"])
    proc = fence.run_fence(argv)
    assert proc.returncode == 0, proc.stderr
    sentinel = fence.protected[".claude"] / "sentinel"
    assert not sentinel.exists()
    assert "--ro-bind" in argv  # only that one overlay was removed


@requires_bwrap
def test_a_symlinked_protected_path_does_not_stop_the_launch(tmp_path: Path) -> None:
    """A symlink bound at its own path aborts the launch; resolved, it does not.

    Both symlinked protected paths are exercised at once: one resolving into
    another protected directory (the fence's own bytes already seal it, so no
    overlay of its own is composed) and one resolving to a file outside every
    protected path (resolved and bound at its target, because the link's path
    cannot be a mount destination).
    """
    fence = SymlinkFence(tmp_path)
    argv = fence.argv()

    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    assert "--ro-bind" in argv
    # No mount destination has a symlink anywhere in its ancestry: bubblewrap
    # refuses a point below a link exactly as it refuses a symlinked leaf.
    for destination in _bind_destinations(argv):
        assert not _has_symlink_component(destination), destination
    # The symlink into another protected directory carries no overlay at all —
    # neither at the link nor at the target, which that directory already
    # seals — and the clause has its own observable rather than only the
    # link's absence from argv.
    assert str(fence.protected[".gitconfig"]) not in argv
    assert str(_backends.resolved_destination(fence.inside_target)) not in argv
    assert _overlays_inside_another(argv) == []
    # The one outside every protected path needs its resolved target bound.
    assert str(fence.outside_target) in argv

    proc = fence.run_fence(argv)
    assert proc.returncode == 0, proc.stderr
    results = fence.results_text()

    # Positive control: only the stub, running inside the fence, writes results.
    assert "symlink-write-refused" in results
    for link in fence.symlinks():
        assert f"symlink-write-refused {link}" in results, link
    # The refusal is the read-only overlay, not a missing file: the shell says so.
    assert "Read-only file system" in proc.stderr

    for grant in fence.grants():
        assert f"grant-write-ok {grant}" in results, grant
        assert (grant / ".fence-write-probe").exists(), grant


@requires_bwrap
def test_the_negative_control_binds_a_symlink_and_the_launch_is_refused(
    tmp_path: Path,
) -> None:
    """The declared mutation: undo the resolution, and the launch must not start.

    The mutated argv carries the overlay at the symlink's own path; bubblewrap
    refuses that mount, so the stub never runs and the exit is nonzero. If the
    mutation failed to apply the launch would succeed and this test would fail,
    so the refusal is attributable to the removed resolution.
    """
    fence = SymlinkFence(tmp_path)
    argv = _bind_at_the_unresolved_path(
        fence.argv(), fence.protected[".netrc"], fence.outside_target
    )
    # The mutation applied: the link is now a destination, the target is not.
    assert str(fence.protected[".netrc"]) in argv
    assert str(fence.outside_target) not in argv

    proc = fence.run_fence(argv)
    assert proc.returncode != 0
    assert not fence.results.exists()


@requires_bwrap
def test_a_protected_path_below_a_symlinked_directory_does_not_stop_the_launch(
    tmp_path: Path,
) -> None:
    """A link *above* a protected path aborts the launch; resolved, it does not.

    The shape the leaf-only resolution missed: ``.config`` is the symlink and
    ``reckon`` below it is an ordinary directory, so a check on the leaf's own
    ``is_symlink()`` never fires and bubblewrap refuses the destination with
    ``Can't mkdir``. The run's own write root also lives below the link, so the
    same home shows the writable grants are resolved with the overlays rather
    than lost to the containment test.
    """
    fence = AncestorSymlinkFence(tmp_path)
    argv = fence.argv()

    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    for destination in _bind_destinations(argv):
        assert not _has_symlink_component(destination), destination
    # Each protected child is bound at the real directory behind the link, and
    # the path as the fence spelled it is nowhere in the argv.
    for link in fence.linked_protected():
        assert str(_backends.resolved_destination(link)) in argv, link
        assert str(link) not in argv, link
    # The grant below the link is present, so resolving the roots kept it.
    assert str(_backends.resolved_destination(fence.run)) in argv

    proc = fence.run_fence(argv)
    assert proc.returncode == 0, proc.stderr
    results = fence.results_text()

    # A write through the symlinked directory is refused, sentinel and all.
    assert "protected-rm-refused" in results
    assert (fence.config / "flight.toml").exists()
    for root in fence.linked_protected():
        sentinel = root / "sentinel"
        assert sentinel.exists(), sentinel
        assert f"protected-rm-refused {sentinel}" in results, sentinel

    for grant in fence.grants():
        assert f"grant-write-ok {grant}" in results, grant
        assert (grant / ".fence-write-probe").exists(), grant


@requires_bwrap
def test_the_negative_control_restores_leaf_only_resolution(tmp_path: Path) -> None:
    """The declared mutation: leaf-only resolution, and the launch must not start.

    The mutated argv binds a protected path below the symlinked directory at its
    own unresolved path; bubblewrap refuses to create that mount point, the stub
    never runs and the exit is nonzero. The mutation is asserted to have applied
    first, so the refusal is attributable to the missing resolution.
    """
    fence = AncestorSymlinkFence(tmp_path)
    link = fence.protected[".config/reckon"]
    resolved = _backends.resolved_destination(link)
    argv = _bind_at_the_unresolved_path(fence.argv(), link, resolved)

    # The mutation applied: the unresolved path is now a destination, the
    # resolved one is not, and it is reached through a link.
    assert str(link) in argv
    assert str(resolved) not in argv
    assert _has_symlink_component(link)

    proc = fence.run_fence(argv)
    assert proc.returncode != 0
    assert not fence.results.exists()


@requires_bwrap
def test_the_negative_control_rebinds_a_target_inside_a_sealed_directory(
    tmp_path: Path,
) -> None:
    """The declared mutation: the inside-a-protected-path skip, removed.

    A second read-only overlay of a path an already-sealed directory covers is
    accepted by bubblewrap, so the clause cannot be caught by executing the
    argv. Its observable is the argv itself: the freshly bound target reads as
    an overlay another destination already covers. Applied first, the mutation
    is shown to flip exactly that observable — without this the clause rested on
    the link's absence from argv, which a redundant bind does not change.
    """
    fence = SymlinkFence(tmp_path)
    argv = fence.argv()
    target = _backends.resolved_destination(fence.inside_target)

    # The clause holds at the head: the sealed directory covers the target and
    # no second overlay of it is composed.
    assert _overlays_inside_another(argv) == []
    assert str(target) not in argv

    mutated = _bind_again(argv, target)
    assert str(target) in mutated  # the mutation applied
    assert _overlays_inside_another(mutated) == [target]


_RED_LOGS = {
    "protected-overlay": (NEGATIVE_CONTROL_MUTATION, _negative_control_report),
    "symlink-resolution": (
        SYMLINK_NEGATIVE_CONTROL_MUTATION,
        _symlink_negative_control_report,
    ),
    "symlinked-ancestor": (
        ANCESTOR_LEAF_ONLY_MUTATION,
        _ancestor_negative_control_report,
    ),
    "redundant-overlay": (
        REDUNDANT_OVERLAY_MUTATION,
        _redundant_overlay_control_report,
    ),
}


if __name__ == "__main__":  # pragma: no cover - reproduces the red logs
    selected = sys.argv[1] if len(sys.argv) > 1 else "protected-overlay"
    mutation, report = _RED_LOGS[selected]
    print(mutation)
    with tempfile.TemporaryDirectory() as directory:
        for line in report(Path(directory)):
            print(line)
    sys.exit(0)
