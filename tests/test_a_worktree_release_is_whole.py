"""A worktree is released whole or not at all.

``git worktree remove`` deletes a tree's files while deregistering it under one
deadline; on a loaded filesystem that deadline has expired partway, leaving a
registered tree holding a fraction of its files — neither whole nor gone. Every
removal site now vacates the registered path in a single rename onto a trash
directory before git is told to forget the tree, so a deadline or a failure
during the copy's deletion can only leave trash, which ``crew gc`` sweeps.

Every case works on a synthesised repository under a temporary config home; no
real fleet directory is read or written.
"""

from __future__ import annotations

import ast
import importlib
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon.crew.runs import _write_json, pointer_path, run_dir, runs_dir
from tests.helpers.git import git as _git

routing_module = importlib.import_module("reckon.crew.routing")
promotion_release_module = importlib.import_module("reckon.crew.promotion_release")
velocity_module = importlib.import_module("reckon.velocity")

PROJECT = "worktree-release-fixture"
SESSION = "worktree-release-session"
RELEASE_HELPER = "_release_worktree_whole"

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# The removal sites the one helper must own. Named by module and function so a
# site that stops routing through the helper — or a new one that never did — is
# named rather than counted.
EXPECTED_HELPER_CALLERS = {
    "reckon/crew/routing.py": {
        "_save_and_release_worktree",
        "garbage_collect",
        "_remove_worktree",
    },
    "reckon/crew/promotion_release.py": {"_release_run_workspace"},
}


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for index in range(6):
        (repo / f"tracked-{index}.txt").write_text(f"file {index}\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "."),
        ("commit", "-q", "-m", "test: seed the worktree-release fixture"),
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _worktree(repo: Path, name: str) -> Path:
    """A managed tree at the same head as the repository, clean and integrated."""
    root = routing_module._workspace_roots(repo)[0]
    tree = root / SESSION / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def _registered(repo: Path) -> set[str]:
    return {
        str(Path(line.removeprefix("worktree ")).resolve())
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }


def _delete_half(tree: Path) -> int:
    """Delete about half the regular files under ``tree``, returning the count.

    Simulates a deletion that stops partway: the instrument that sees a healthy
    run must first see something present, so this returns a positive count
    rather than nothing.
    """
    files = [path for path in sorted(tree.rglob("*")) if path.is_file()]
    removed = 0
    for path in files[: max(1, len(files) // 2)]:
        path.unlink()
        removed += 1
    return removed


def _trash_entries(repo: Path) -> list[Path]:
    root = (
        routing_module._workspace_roots(repo)[0] / routing_module.WORKTREE_TRASH_DIRNAME
    )
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _record(repo: Path, tree: Path, run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "worktree": str(tree),
        "repo": str(repo),
        "pid": _dead_pid(),
    }


def test_a_release_whose_deletion_times_out_leaves_no_partial_tree_and_gc_sweeps_its_trash(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _worktree(repository, "timeout-release")
    run_id = "r-20261012T050000000000-timeout-release"

    # The head's deletion step is the trash copy's removal. A partial deletion
    # that times out there must leave the original path empty.
    def _partial_then_timeout(trash: Path) -> None:
        assert _delete_half(trash) > 0, "the deletion made nothing happen"
        raise subprocess.TimeoutExpired(cmd=["delete", str(trash)], timeout=180)

    monkeypatch.setattr(routing_module, "_delete_worktree_copy", _partial_then_timeout)

    # The pre-change deletion step ran `git worktree remove` under velocity's
    # 180 s deadline. This arm fires only if that step is restored, deleting part
    # of the tree and then timing out exactly as the incident recorded.
    real_run_git = velocity_module.run_git

    def _timeout_on_git_remove(repo, *args, **kwargs):
        if "worktree" in args and "remove" in args:
            _delete_half(Path(args[-1]))
            raise subprocess.TimeoutExpired(
                cmd=["git", "worktree", "remove"], timeout=180
            )
        return real_run_git(repo, *args, **kwargs)

    monkeypatch.setattr(velocity_module, "run_git", _timeout_on_git_remove)

    try:
        result = promotion_release_module._release_run_workspace(
            _record(repository, tree, run_id)
        )
    except subprocess.TimeoutExpired:
        result = {"worktree_released": False, "worktree_withheld": "removal timed out"}

    assert result["worktree_released"] is True, result
    assert not tree.exists(), "a partial tree was left at the registered path"
    assert str(tree) not in _registered(repository), "the worktree was not deregistered"
    trash_left = str(result.get("worktree_trash_left") or "")
    assert trash_left, result
    assert Path(trash_left).is_dir()

    report = routing_module.garbage_collect(
        repo=repository, project=PROJECT, apply=True
    )
    assert report["worktree_trash_removed"] == 1, report["worktree_trash"]
    assert not Path(trash_left).exists()
    assert _trash_entries(repository) == []


def test_a_normal_release_removes_and_deregisters_the_tree(repository: Path) -> None:
    tree = _worktree(repository, "normal-release")
    run_id = "r-20261012T050000000000-normal-release"
    marker = tree / "tracked-0.txt"
    assert marker.is_file()

    result = promotion_release_module._release_run_workspace(
        _record(repository, tree, run_id)
    )

    assert result["worktree_released"] is True, result
    assert not tree.exists()
    assert str(tree) not in _registered(repository)
    assert "worktree_trash_left" not in result
    assert _trash_entries(repository) == []


def test_a_live_claimed_tree_is_not_moved(repository: Path) -> None:
    tree = _worktree(repository, "parked-share")
    run_id = "r-20261012T050000000000-parked-share"
    peer_id = "r-20261012T050000000001-parked-peer"
    marker = tree / "tracked-0.txt"
    # A peer parked between turns: its pointer carries a terminal-looking phase
    # while it still names this tree, so the wide claim protects it.
    _write_json(
        Path(pointer_path(peer_id)),
        {
            "run_id": peer_id,
            "pid": _dead_pid(),
            "phase": "complete",
            "worktree": str(tree),
            "repo": str(repository),
            "project": PROJECT,
            "node": "parked-peer",
        },
    )
    run_dir(peer_id).mkdir(parents=True, exist_ok=True)

    result = promotion_release_module._release_run_workspace(
        _record(repository, tree, run_id)
    )

    assert result["worktree_released"] is False, result
    assert tree.is_dir() and marker.is_file()
    assert str(tree) in _registered(repository)
    assert _trash_entries(repository) == []
    with pytest.raises(routing_module.CrewError):
        routing_module._remove_worktree(repository, str(tree))
    assert tree.is_dir() and marker.is_file()


def test_an_unreclaimable_tree_is_not_moved(repository: Path) -> None:
    tree = _worktree(repository, "dirty-tree")
    run_id = "r-20261012T050000000000-dirty-tree"
    (tree / "untracked.txt").write_text("uncommitted\n", encoding="utf-8")

    result = promotion_release_module._release_run_workspace(
        _record(repository, tree, run_id)
    )

    assert result["worktree_released"] is False, result
    assert tree.is_dir()
    assert str(tree) in _registered(repository)
    assert _trash_entries(repository) == []


def test_releasing_one_tree_leaves_a_peer_registration_whose_directory_vanished(
    repository: Path,
) -> None:
    """A vanish-outside-git registration is another session's record and survives.

    The released tree's own registration is forgotten, but a global
    ``git worktree prune`` would also drop the registration of a peer tree whose
    directory was removed outside git — the record gc reports and leaves in
    place — so releasing one tree must not touch another's.
    """
    released = _worktree(repository, "released-tree")
    vanished = _worktree(repository, "vanished-tree")
    run_id = "r-20261012T050000000000-released-tree"
    # The directory is gone but git still registers the tree: exactly the state
    # a removal by something outside git leaves behind.
    shutil.rmtree(vanished)
    assert str(vanished) in _registered(repository)

    result = promotion_release_module._release_run_workspace(
        _record(repository, released, run_id)
    )

    assert result["worktree_released"] is True, result
    assert str(released) not in _registered(repository)
    assert str(vanished) in _registered(repository), (
        "releasing one tree dropped a peer's registration"
    )


def test_a_pointer_naming_the_repository_root_is_withheld_and_the_checkout_survives(
    repository: Path,
) -> None:
    """The checkout is not a disposable tree, however a pointer names it.

    A run record whose worktree is the repository itself reached the deletion
    step before the move-aside existed, and only ``git worktree remove``'s
    refusal of the main worktree kept the checkout alive. The rename has no such
    refusal, so the path is checked before anything moves.
    """
    run_id = "r-20261012T050000000000-checkout-pointer"
    result = promotion_release_module._release_run_workspace(
        _record(repository, repository, run_id)
    )

    assert not result.get("worktree_released"), result
    assert "repository root" in str(result.get("worktree_withheld")), result
    assert (repository / ".git").exists(), "the checkout lost its git directory"
    assert (repository / "tracked-0.txt").is_file(), "the checkout lost its files"
    assert (repository / "docs" / "state" / PROJECT).is_dir()
    assert _trash_entries(repository) == []
    assert str(repository) in _registered(repository)


def test_a_registered_tree_outside_the_worktree_root_is_withheld(
    repository: Path, tmp_path: Path
) -> None:
    """A registered worktree outside the managed root is not this tool's to take.

    Registration alone is not the licence to move a tree: a tree git knows about
    but the workspace does not manage is left alone, and the refusal names the
    root rather than the registration.
    """
    outsider = tmp_path / "outside-root"
    _git(repository, "worktree", "add", "-q", "--detach", str(outsider), "HEAD")
    keeper = outsider / "tracked-0.txt"
    assert keeper.is_file()
    assert str(outsider) in _registered(repository)

    released = routing_module._release_worktree_whole(repository, outsider)

    assert released["released"] is False, released
    assert "outside the managed worktree root" in released["withheld"], released
    assert keeper.is_file()
    with pytest.raises(routing_module.CrewError):
        routing_module._remove_worktree(repository, str(outsider))
    assert keeper.is_file()
    assert str(outsider) in _registered(repository)
    assert _trash_entries(repository) == []


def test_a_path_that_is_no_worktree_at_all_is_withheld(
    repository: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "plain-directory"
    plain.mkdir()
    keeper = plain / "keeper.txt"
    keeper.write_text("not a worktree\n", encoding="utf-8")

    released = routing_module._release_worktree_whole(repository, plain)

    assert released["released"] is False, released
    assert "not registered" in released["withheld"], released
    assert keeper.is_file()
    assert _trash_entries(repository) == []


def _runs_root_tree(repository: Path, name: str) -> Path:
    """A registered worktree under the crew runs root, as a worker creates one."""
    tree = runs_dir() / name / "source"
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def test_a_registered_worktree_under_the_runs_root_is_released_and_reported_removed(
    repository: Path,
) -> None:
    """gc walks the runs root, so a tree a worker left there must be releasable.

    The managed root alone is not the whole managed set: a worker that runs its
    own ``git worktree add`` inside its run directory leaves a registered tree
    under the crew runs root, and gc's candidate filter admits it. A release
    that refused it would report the tree removed while it stayed on disk.
    """
    tree = _runs_root_tree(repository, "r-20261012T050000000000-runs-root-tree")
    assert str(tree) in _registered(repository)

    report = routing_module.garbage_collect(
        repo=repository, project=PROJECT, apply=True
    )

    assert str(tree) in report["removed_worktrees"], report["withheld_worktrees"]
    assert report["withheld_worktrees"] == []
    assert not tree.exists()
    assert str(tree) not in _registered(repository)
    assert list((runs_dir() / "_trash").glob("*")) == []


def test_gc_reports_a_withheld_release_and_leaves_it_out_of_removed(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release the helper refuses is a withheld row, never a removal.

    The helper reports a refusal by returning rather than raising, so a caller
    that ignores the flag reports a tree as removed that is still on disk.
    """
    tree = _runs_root_tree(repository, "r-20261012T050000000001-refused-tree")
    reason = "worktree lies outside the managed worktree root"
    monkeypatch.setattr(
        routing_module,
        "_release_worktree_whole",
        lambda repo, path, **kwargs: {
            "path": str(path),
            "released": False,
            "withheld": reason,
        },
    )

    report = routing_module.garbage_collect(
        repo=repository, project=PROJECT, apply=True
    )

    assert report["withheld_worktrees"] == [{"worktree": str(tree), "reason": reason}]
    assert str(tree) not in report["removed_worktrees"]
    assert tree.is_dir(), "a withheld release left the tree removed anyway"
    assert str(tree) in _registered(repository)


def _literal_argv(call: ast.Call) -> list[str]:
    values: list[str] = []
    for argument in call.args:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            values.append(argument.value)
        elif isinstance(argument, (ast.List, ast.Tuple)):
            values.extend(
                element.value
                for element in argument.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return values


def _calls_helper(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == RELEASE_HELPER
        for child in ast.walk(node)
    )


def _calls_git_remove(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        argv = _literal_argv(child)
        if any(
            first == "worktree" and second == "remove"
            for first, second in itertools.pairwise(argv)
        ):
            return True
    return False


def _functions(source: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_every_removal_site_goes_through_the_one_helper() -> None:
    found: dict[str, set[str]] = {}
    for relative in EXPECTED_HELPER_CALLERS:
        source = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
        found[relative] = {
            node.name for node in _functions(source) if _calls_helper(node)
        }
    assert found == EXPECTED_HELPER_CALLERS, found


def test_no_removal_site_still_deletes_with_git_worktree_remove() -> None:
    offenders = [
        f"{source_path.name}::{node.name}"
        for source_path in sorted((REPOSITORY_ROOT / "reckon").rglob("*.py"))
        for node in _functions(source_path.read_text(encoding="utf-8"))
        if _calls_git_remove(node)
    ]
    assert offenders == [], offenders
