"""The fence never re-opens the main checkout, and seeds a home before it binds.

Two defects in the fenced launch composition, both proved through the production
path — ``launch_plan`` with the fence roots ``fenced_write_roots`` returns, the
way a dispatcher folds them — never ``fence_argv`` with its writable roots
filled in by hand.

* A worker's declared write paths are repo-relative. Resolved against the
  *repository* they name the main checkout, so the fence re-binds roots inside
  the very checkout it seals; resolved against the worker's *worktree* they sit
  inside a tree that is already writable and are skipped. Only a worktree's own
  git bookkeeping (its ``.git/worktrees/<name>`` directory and the shared object
  store) may be re-opened under the main checkout, because a commit has to write
  them.
* A manifest may be declared in a directory that does not exist yet. When the
  fence creates its write roots *after* resolving the harness home, the run
  directory is still missing at that moment, no home is seeded, and the harness
  config variable is left unset — so a fenced codex launch falls back to the
  sealed operator ``~/.codex`` and exits "Read-only file system". Creating the
  roots first gives the run its home and its variable.

The declared mutation resolves a relative declared path against the repository
again, and the no-main-checkout-bind assertion must then fail. The red run is
recorded under the run directory named in the manifest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import _backends

# Three declarations of the shape a node's prompt carries: repo-relative files,
# each with a suffix, so each is granted through its parent directory.
DECLARED = ("reckon/crew/x.py", "tests/test_x.py", "docs/plans/p.html")

PROJECT = "proj"


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


class Repository:
    """A synthesised main checkout, a detached worktree, and a live run.

    The main checkout sits under the stand-in home's ``Code`` root, so the fence
    seals it; the worktree sits under the separate worktrees root, outside every
    seal, exactly where dispatch places one.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.config = self.home / ".config" / "reckon"
        self.reports = self.config / "reports"
        self.reviews = self.config / "reviews"
        self.run = self.config / "crew" / "runs" / "r1"
        for directory in (self.reports, self.reviews, self.run):
            directory.mkdir(parents=True)
        self.checkout = self.home / "Code" / PROJECT
        self.checkout.mkdir(parents=True)
        _git("init", "-q", cwd=self.checkout)
        _git("config", "user.email", "t@example.com", cwd=self.checkout)
        _git("config", "user.name", "t", cwd=self.checkout)
        _git("config", "commit.gpgsign", "false", cwd=self.checkout)
        (self.checkout / "README").write_text("x")
        _git("add", "README", cwd=self.checkout)
        _git("commit", "-q", "-m", "init", cwd=self.checkout)
        self.worktrees = self.home / "Code" / ".reckon-worktrees" / "proj-abc"
        self.worktrees.mkdir(parents=True)
        self.worktree = self.worktrees / "node"
        _git("worktree", "add", "--detach", str(self.worktree), cwd=self.checkout)
        self.manifest = self.run / "manifest.md"

    def compose(
        self,
        *,
        dialect: str = "claude",
        sandbox: str = _backends.WORKTREE_FULL,
        manifest: Path | None = None,
    ) -> _backends.LaunchPlan:
        """Compose a launch the way a dispatch does: fence roots, then plan."""
        manifest = self.manifest if manifest is None else manifest
        roots = _backends.fenced_write_roots(
            {"launch": "cli", "sandbox": sandbox},
            repository=self.checkout,
            run_directory=manifest.parent,
            reports_directory=self.reports,
            review_store_directory=self.reviews,
            manifest_path=manifest,
            declared_write_paths=DECLARED,
            worktree=self.worktree,
        )
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": "true",
                "dialect": dialect,
                "sandbox": sandbox,
            },
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(manifest),
            writable_directories=roots,
            fence=True,
            fence_home=str(self.home),
        )


def _writable_binds(argv: list[str]) -> list[Path]:
    """Return every writable mount destination in a composed fence argv."""
    destinations: list[Path] = []
    for index, flag in enumerate(argv):
        if flag == "--bind" and index + 2 < len(argv):
            destinations.append(Path(argv[index + 2]))
    return destinations


def _under(destination: Path, root: Path) -> bool:
    return destination == root or root in destination.parents


def test_a_repo_relative_declaration_never_binds_the_main_checkout(
    tmp_path: Path,
) -> None:
    """A declared path names a file in the worker's worktree, not the checkout."""
    repository = Repository(tmp_path)

    plan = repository.compose()

    binds = _writable_binds(plan.argv)
    under_checkout = [
        destination for destination in binds if _under(destination, repository.checkout)
    ]
    # Positive control: the worktree's git bookkeeping is under the main
    # checkout and *is* bound, so an empty result would mean the check is blind
    # rather than that the checkout is sealed. Only those two may be re-opened.
    git_roots = set(_backends.worktree_git_write_roots(repository.worktree))
    assert git_roots, "the fixture did not produce a linked worktree's git roots"
    assert set(under_checkout) == git_roots, (
        "the fence re-opened the main checkout at "
        f"{sorted(set(under_checkout) - git_roots)}"
    )


@pytest.mark.parametrize(
    ("dialect", "variable", "folder"),
    [
        ("claude", "CLAUDE_CONFIG_DIR", "harness"),
        ("codex", "CODEX_HOME", "codex-home"),
    ],
)
def test_a_missing_manifest_directory_still_gets_its_harness_home(
    tmp_path: Path,
    dialect: str,
    variable: str,
    folder: str,
) -> None:
    """The run's roots are created before the home is resolved and seeded."""
    repository = Repository(tmp_path)
    run = repository.config / "crew" / "runs" / "absent"
    manifest = run / "manifest.md"
    assert not run.exists(), "the fixture must start with the run directory missing"

    plan = repository.compose(dialect=dialect, manifest=manifest)

    assert run.is_dir(), "the manifest's directory was not created"
    home = Path(plan.environment[variable])
    assert home == run / folder
    assert home.is_dir(), (
        f"{variable} names {home}, which does not exist, so a fenced {dialect} "
        "worker would fall back to the sealed operator home"
    )
