"""gc clears an abandoned clean tree and an already-removed one.

A deliberately abandoned worktree is often left *clean*: its worker carried
commits with no patch-equivalent on the integration head, then stopped without
touching the working tree. gc measured the commits beyond the integration head
only for a dirty tree, so such a tree was classified ``unintegrated`` with its
commits never read, and ``--pin-unique-commits`` released only ``dirty`` rows: a
clean abandoned tree had no route through gc at all. Under the flag gc now
measures the commits of a clean unreachable tree too, pins them under
``refs/reckon/archive/<node id>``, and releases the tree; without the flag the
tree is still withheld.

The second gap is the already-removed one. After a caller pinned the head and
removed the tree by hand, ``gc --run <id>`` refused with "which is not a
worktree this repository registers". A recorded tree that is no longer
registered is the ordinary end of a run's life, not a refusal: the sweep reports
it as already-removed, clears the run's lock, and exits 0.

Every case works on a synthesised repository, ledger and pointer under a
temporary config home; no real fleet directory is read or written.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, ledger
from reckon.crew.runs import _write_json, crew_home, pointer_path
from tests.helpers.git import git as _git

routing_module = importlib.import_module("reckon.crew.routing")

PROJECT = "abandoned-clean-tree-gc-fixture"
SESSION = "gc-fixture-session"
RUN_RETAINED = "r-20261013T120048839650-retained-node"
RUN_LIVE = "r-20261013T120048839651-live-node"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the gc fixture"),
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _abandoned(repository: Path, name: str) -> tuple[Path, str]:
    """A clean worktree carrying one commit that exists nowhere else."""
    root = routing_module._workspace_roots(repository)[0]
    tree = root / SESSION / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    (tree / "only-here.py").write_text("work that exists nowhere else\n")
    _git(tree, "add", "only-here.py")
    _git(tree, "commit", "-q", "-m", "test: work that exists nowhere else")
    assert _git(tree, "status", "--porcelain").strip() == ""
    return tree, _git(tree, "rev-parse", "HEAD")


def _retained(repository: Path, run_id: str, tree: Path) -> None:
    """A promoted run's ledger row whose retained tree is named by the run."""
    record = ledger.build_record(
        run_id=run_id,
        plan="fixture-plan",
        gate="passed",
        project=PROJECT,
        node=tree.name,
    )
    record["worktree"] = str(tree.resolve())
    ledger.append_run(PROJECT, record, root=repository, allow_create=True)


def _park(repository: Path, run_id: str, tree: Path) -> None:
    """A live pointer for a run parked between turns, naming ``tree``."""
    pointer = {
        "run_id": run_id,
        "pid": _dead_pid(),
        "worktree": str(tree),
        "repo": str(repository),
        "project": PROJECT,
        "node": tree.name,
        "phase": "complete",
    }
    _write_json(Path(pointer_path(run_id)), pointer)


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _resolves(root: Path, revision: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", revision],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def test_a_clean_unreachable_tree_is_withheld_without_the_flag(repo: Path) -> None:
    """Without the pin the abandoned clean tree is kept, and no ref is written."""
    tree, head = _abandoned(repo, "abandoned")
    ref = f"refs/reckon/archive/{tree.name}"

    report = routing_module.garbage_collect(repo=repo, project=PROJECT, apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["classification"] == "unintegrated"
    assert row["head"] == head
    assert row["non_equivalent_commits"], "the unique commit must be measured"
    assert row["reclaimable"] is False
    assert _resolves(repo, ref) is None
    assert str(tree) not in report["removed_worktrees"]
    assert tree.is_dir()
    assert (tree / "only-here.py").read_text() == "work that exists nowhere else\n"


def test_the_flag_pins_and_releases_a_clean_unreachable_tree(repo: Path) -> None:
    """With the pin the clean tree's commits are archived and the tree released."""
    tree, head = _abandoned(repo, "abandoned")
    ref = f"refs/reckon/archive/{tree.name}"

    report = routing_module.garbage_collect(
        repo=repo, project=PROJECT, apply=True, pin_unique_commits=True
    )
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["reclaimable"] is True
    assert row["archive_ref"] == ref
    assert row["head"] == head
    assert str(tree) in report["removed_worktrees"]
    assert not tree.is_dir()

    assert _resolves(repo, f"{ref}^{{commit}}") == head
    assert _git(repo, "show", f"{ref}:only-here.py") == "work that exists nowhere else"


def test_the_dry_run_reports_the_reclaim_before_the_flag_acts(repo: Path) -> None:
    """A dry run states the pin makes the clean tree reclaimable, and moves nothing."""
    tree, _ = _abandoned(repo, "abandoned")
    ref = f"refs/reckon/archive/{tree.name}"

    report = routing_module.garbage_collect(
        repo=repo, project=PROJECT, apply=False, pin_unique_commits=True
    )
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert report["dry_run"] is True
    assert row["reclaimable"] is True
    assert report["removed_worktrees"] == []
    assert _resolves(repo, ref) is None
    assert tree.is_dir()


def test_a_live_claimed_clean_tree_is_never_pinned_or_removed(repo: Path) -> None:
    """A live pointer holds the tree even under the pin: the claim is checked first."""
    tree, _ = _abandoned(repo, "live-node")
    _park(repo, RUN_LIVE, tree)
    ref = f"refs/reckon/archive/{tree.name}"

    report = routing_module.garbage_collect(
        repo=repo, project=PROJECT, apply=True, pin_unique_commits=True
    )
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))

    assert row["classification"] == "live-referenced"
    assert row["claimed_by_live_runs"] == [RUN_LIVE]
    assert row["reclaimable"] is False
    assert _resolves(repo, ref) is None
    assert tree.is_dir()
    assert str(tree) not in report["removed_worktrees"]


def test_gc_run_on_a_hand_removed_tree_reports_already_removed(repo: Path) -> None:
    """A recorded tree that is gone is already removed: exit 0, lock cleared."""
    tree, _ = _abandoned(repo, "retained-node")
    _retained(repo, RUN_RETAINED, tree)
    _git(repo, "worktree", "remove", "--force", str(tree))
    assert not tree.exists()

    lock = crew_home() / "locks" / f"{RUN_RETAINED}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    assert lock.is_file()

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(repo),
            "--project",
            PROJECT,
            "--run",
            RUN_RETAINED,
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["already_removed"] is True
    assert payload["already_removed_run"] == RUN_RETAINED
    assert payload["already_removed_worktree"] == str(tree.resolve())
    assert payload["removed_worktrees"] == []

    assert not lock.exists()
    assert payload["run_lock"] == [
        {
            "run_id": RUN_RETAINED,
            "path": str(lock),
            "action": "prune",
            "removed": True,
        }
    ]


def test_gc_run_on_a_hand_removed_tree_without_apply_removes_nothing(
    repo: Path,
) -> None:
    """The dry run reports already-removed and leaves the lock in place."""
    tree, _ = _abandoned(repo, "retained-node")
    _retained(repo, RUN_RETAINED, tree)
    _git(repo, "worktree", "remove", "--force", str(tree))

    lock = crew_home() / "locks" / f"{RUN_RETAINED}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(repo),
            "--project",
            PROJECT,
            "--run",
            RUN_RETAINED,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["already_removed"] is True
    assert lock.is_file()
    assert payload["run_lock"][0]["removed"] is False
