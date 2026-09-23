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


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory)):
            print(line)
    sys.exit(0)
