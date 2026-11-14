"""A node with no declared write paths may write anywhere git can merge.

A worktree isolates every edit, so the only writes it cannot reconcile are the
paths git cannot merge textually: a plan document, generated state, a
cumulative evidence record, a figure topic directory and a dependency lock. A
node that declares no write paths is therefore granted its whole worktree
except those, and a write to one of them is refused by a read-only bind whose
governing pattern the composed launch names.

A node that declares write paths is fenced exactly as before: nothing inside its
worktree is sealed by this rule.

The third case is the negative control: fencing a no-write-paths worker to
nothing refuses the source-file write, which shows the source write's success
is the worktree grant doing its work rather than a container that happened to
allow it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _backends, _worker_fence
from reckon.crew.runs import unmergeable_patterns

# One write per exclusive category, each with the pattern that governs it.
# ``docs/plans/*.html`` names a file that does not exist yet, so the case shows
# a *new* name under the pattern is refused too.
UNMERGEABLE = (
    ("docs/plans/*.html", "docs/plans/another-plan.html"),
    ("docs/state/", "docs/state/proj/index.json"),
    ("docs/evidence/archive/", "docs/evidence/archive/a-doc-landed.html"),
    ("docs/figures/", "docs/figures/topic/plot.svg"),
    ("uv.lock", "uv.lock"),
)

WRITABLE = ("reckon/crew/demo.py", "tests/test_demo.py")

DECLARED = ("reckon/crew/demo.py", "tests/test_demo.py")


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=str(cwd), check=True, capture_output=True)


class Repository:
    """A synthesised checkout, a worktree, a run directory and its sealed home."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.config = self.home / ".config" / "reckon"
        self.reports = self.config / "reports"
        self.reviews = self.reports / "reviews"
        self.run = self.config / "crew" / "runs" / "r1"
        for directory in (self.reports, self.reviews, self.run):
            directory.mkdir(parents=True)
        self.checkout = self.home / "Code" / "proj"
        self.checkout.mkdir(parents=True)
        self.worktree = self.home / "Code" / ".reckon-worktrees" / "proj-abc" / "node"
        self.worktree.parent.mkdir(parents=True)
        self.manifest = self.run / "manifest.md"
        self._build_worktree()

    def _build_worktree(self) -> None:
        _git("init", "-q", cwd=self.checkout)
        _git("config", "user.email", "t@example.com", cwd=self.checkout)
        _git("config", "user.name", "t", cwd=self.checkout)
        _git("config", "commit.gpgsign", "false", cwd=self.checkout)
        _git("config", "user.name", "t", cwd=self.checkout)
        (self.checkout / "README").write_text("x")
        _git("add", "README", cwd=self.checkout)
        _git("commit", "-q", "-m", "init", cwd=self.checkout)
        _git("worktree", "add", "--detach", str(self.worktree), cwd=self.checkout)
        for relative in (
            "reckon/crew",
            "tests",
            "docs/plans",
            "docs/state/proj",
            "docs/evidence/archive",
            "docs/figures/topic",
        ):
            (self.worktree / relative).mkdir(parents=True, exist_ok=True)
        (self.worktree / "docs" / "plans" / "existing.html").write_text("<html/>")
        (self.worktree / "uv.lock").write_text("version = 1\n")

    def compose(
        self, *, declared: tuple[str, ...] = (), seal: bool = True
    ) -> _backends.LaunchPlan:
        """Compose a launch through the composition a dispatch uses."""
        sandbox = _backends.WORKTREE_FULL
        roots = _backends.fenced_write_roots(
            {"launch": "cli", "sandbox": sandbox},
            repository=str(self.checkout),
            run_directory=str(self.run),
            reports_directory=str(self.reports),
            review_store_directory=str(self.reviews),
            manifest_path=str(self.manifest),
            declared_write_paths=declared,
            worktree=str(self.worktree),
        )
        patterns = (
            unmergeable_patterns(None, self.worktree) if seal and not declared else ()
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
            manifest_path=str(self.manifest),
            writable_directories=roots,
            fence=True,
            fence_home=str(self.home),
            unmergeable_patterns=patterns,
        )


def _fence_prefix(argv: list[str]) -> list[str]:
    """The mounts of a composed fence, up to and including its separator."""
    return [*argv[: argv.index("--") + 1]]


def _run(
    plan: _backends.LaunchPlan, repository: Repository, target: Path
) -> subprocess.CompletedProcess[str]:
    if shutil.which(_backends.FENCE_BINARY) is None:
        pytest.skip("bubblewrap is not installed")
    if _worker_fence._probe_user_namespace().returncode != 0:
        pytest.skip("user namespaces are unavailable in this sandbox")
    return subprocess.run(
        [*_fence_prefix(plan.argv), "/bin/sh", "-c", f"printf written > {target!s}"],
        cwd=str(repository.worktree),
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_no_write_paths_worker_writes_its_source_and_a_new_test(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path)
    plan = repository.compose()

    assert plan.unmergeable_seals, (
        "the no-write-paths launch sealed nothing, so a refusal could not be "
        "attributed to the pattern governing the path"
    )

    for relative in WRITABLE:
        target = repository.worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = _run(plan, repository, target)
        assert completed.returncode == 0, (
            f"a no-write-paths worker was refused {relative}: {completed.stderr}"
        )
        assert target.read_text() == "written", f"{relative} was not written"


@pytest.mark.parametrize(("pattern", "relative"), UNMERGEABLE)
def test_a_no_write_paths_worker_is_refused_each_unmergeable_path(
    tmp_path: Path, pattern: str, relative: str
) -> None:
    repository = Repository(tmp_path)
    plan = repository.compose()
    target = repository.worktree / relative

    completed = _run(plan, repository, target)

    assert completed.returncode != 0, f"{relative} was writable under {pattern!r}"
    assert "Read-only file system" in completed.stderr, (
        f"{relative} was refused for another reason: {completed.stderr}"
    )
    assert pattern in plan.unmergeable_seals.values(), (
        f"no sealed path names the pattern {pattern!r}; sealed: "
        f"{sorted(plan.unmergeable_seals.values())}"
    )
    assert not target.exists() or target.read_text() != "written", (
        f"{relative} was written despite the seal"
    )


def test_a_declared_node_is_fenced_as_before(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    plan = repository.compose(declared=DECLARED)

    assert plan.unmergeable_seals == {}, (
        "a declared node's worktree was sealed by the no-write-paths rule"
    )
    for _pattern, relative in UNMERGEABLE:
        target = repository.worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = _run(plan, repository, target)
        assert completed.returncode == 0, (
            f"a declared node was refused {relative} inside its own worktree: "
            f"{completed.stderr}"
        )


def test_fencing_a_no_write_paths_worker_to_nothing_refuses_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path)
    plan = repository.compose()
    monkeypatch.setattr(_worker_fence, "worktree_write_grant", lambda _worktree: ())
    plan = repository.compose()

    target = repository.worktree / WRITABLE[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = _run(plan, repository, target)

    assert completed.returncode != 0, (
        "a worker fenced to nothing still wrote its source file"
    )
    assert "Read-only file system" in completed.stderr, completed.stderr
