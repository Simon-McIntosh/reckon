"""Every path that removes a run worktree decides liveness through the wide claim.

A run parked between turns keeps its live pointer while its phase reads
terminal-looking and its worker process is gone. The wide claim
(``_live_pointer_worktrees``) keeps every worktree a live pointer names,
whatever its phase; the phase-gated ``_live_worktree_claims`` reads that parked
pointer as finished, so a removal path that reads it can force-remove a tree
that is still a live run's. This test enumerates the removal paths by parsing
the package rather than from a list — a function is a removal path when its
body invokes a worktree-removal command — and drives each of them directly
against a parked run's worktree.

A path whose removal target is read from the released run's own record
(``record["worktree"]``) is the sanctioned release door: promotion and discard
remove the pointer that names the tree and then release it, which is how a
tree is released at all. That door is driven here too, because a peer parked
on an external wait may name the same tree: the release must then leave it
standing rather than remove it under the peer. Every path discovered here —
releasing or reclaiming — decides liveness through the wide claim, so calling
it directly must leave a parked run's tree in place.

Every case works on synthesised pointers, mounts and repositories under a
temporary config home; no real fleet directory is read or written.
"""

from __future__ import annotations

import ast
import importlib
import itertools
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path, run_dir

routing_module = importlib.import_module("reckon.crew.routing")
promotion_module = importlib.import_module("reckon.crew.promotion")

PROJECT = "wide-claim-removal-fixture"
SESSION = "wide-claim-fixture-session"

WIDE_CLAIM = "_live_pointer_worktrees"
PHASE_GATED_CLAIM = "_live_worktree_claims"

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _dead_pid() -> int:
    """A pid the kernel reports as gone: a reaped child."""
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _park(repository: Path, run_id: str, tree: Path) -> None:
    """A live pointer for a run parked mid-measure, and its waiting manifest."""
    _write_json(
        Path(pointer_path(run_id)),
        {
            "run_id": run_id,
            "pid": _dead_pid(),
            "phase": "complete",
            "worktree": str(tree),
            "repo": str(repository),
            "project": PROJECT,
            "node": tree.name,
        },
    )
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.md").write_text(
        "node: parked-node\n"
        "status: waiting\n"
        "wait_condition: job 1280609 finishes the forward solve\n"
        'wait_probe: ["squeue", "-h", "-j", "1280609"]\n'
        "wait_terminal: exit:0\n"
        "resume_brief: read the job log and continue\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class _RemovalPath:
    key: str
    reads_wide: bool
    reads_phase_gated: bool


def _literal_argv(call: ast.Call) -> list[str]:
    """The literal string arguments of a call, in order, including list argv."""
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


def _is_removal_call(node: ast.AST) -> bool:
    """Whether a call invokes a worktree-removal command.

    Both shapes in the package — ``_git(..., "worktree", "remove", ...)`` and a
    subprocess argv ``["git", "worktree", "remove", ...]`` — carry the literal
    pair in their argument list, so the command is recognised from the call
    itself rather than from the name of the helper that issues it.
    """
    if not isinstance(node, ast.Call):
        return False
    argv = _literal_argv(node)
    return any(
        first == "worktree" and second == "remove"
        for first, second in itertools.pairwise(argv)
    )


def _references(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == name for child in ast.walk(node)
    )


def _scan_source(source: str, module_key: str) -> dict[str, _RemovalPath]:
    """The removal paths in one module's source, keyed by module and function."""
    found: dict[str, _RemovalPath] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_removal_call(child) for child in ast.walk(node)):
            continue
        key = f"{module_key}::{node.name}"
        found[key] = _RemovalPath(
            key=key,
            reads_wide=_references(node, WIDE_CLAIM),
            reads_phase_gated=_references(node, PHASE_GATED_CLAIM),
        )
    return found


def _removal_paths() -> dict[str, _RemovalPath]:
    """Every removal path in the package, discovered by parsing its sources."""
    package = REPOSITORY_ROOT / "reckon"
    paths: dict[str, _RemovalPath] = {}
    for source_path in sorted(package.rglob("*.py")):
        key = source_path.relative_to(REPOSITORY_ROOT).as_posix()
        paths.update(_scan_source(source_path.read_text(encoding="utf-8"), key))
    return paths


def _driver_remove_worktree(fleet: Path, tree: Path) -> None:
    with pytest.raises(CrewError):
        routing_module._remove_worktree(fleet, str(tree))


def _driver_garbage_collect(fleet: Path, tree: Path) -> None:
    report = routing_module.garbage_collect(repo=fleet, project=PROJECT, apply=True)
    rows = {item["path"]: item for item in report["worktrees"]}
    assert rows[str(tree)]["classification"] == "live-referenced"
    assert report["removed_worktrees"] == []


def _driver_save_and_release_worktree(fleet: Path, tree: Path) -> None:
    with pytest.raises(CrewError):
        routing_module._save_and_release_worktree(fleet, tree, "HEAD", None)


RELEASED_RUN = "r-20261003T120048839647-release-subject"


def _driver_release_run_workspace(fleet: Path, tree: Path) -> None:
    """The sanctioned release door, whose own tree a parked peer also names."""
    record = {
        "run_id": RELEASED_RUN,
        "worktree": str(tree),
        "repo": str(fleet),
        "pid": _dead_pid(),
    }
    result = promotion_module._release_run_workspace(record)
    assert result["worktree_released"] is False, result
    assert "live run pointer" in str(result.get("worktree_withheld") or "")


REMOVAL_DRIVERS = {
    "reckon/crew/routing.py::_remove_worktree": _driver_remove_worktree,
    "reckon/crew/routing.py::garbage_collect": _driver_garbage_collect,
    "reckon/crew/routing.py::_save_and_release_worktree": (
        _driver_save_and_release_worktree
    ),
    "reckon/crew/promotion_release.py::_release_run_workspace": (_driver_release_run_workspace),
}


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the removal-path fixture"),
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _worktree(repository: Path, name: str) -> Path:
    """A managed tree at the same head as the repository, clean and integrated."""
    root = routing_module._workspace_roots(repository)[0]
    tree = root / SESSION / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def test_each_removal_path_leaves_a_parked_runs_worktree_in_place(
    fleet: Path,
) -> None:
    """Every removal path, called directly, spares a parked run's tree.

    Each path gets its own parked tree, so one path removing the tree cannot
    mask whether the others would have spared it. The release door is driven
    here as well: its own record names the tree, and the parked peer's pointer
    names the same tree.
    """
    assert REMOVAL_DRIVERS, "no driver was registered; the enumeration lost its subject"
    for key, driver in sorted(REMOVAL_DRIVERS.items()):
        name = key.rsplit("::", 1)[-1].strip("_").replace("_", "-")
        tree = _worktree(fleet, f"parked-{name}")
        _park(fleet, f"r-20261003T120048839648-{name}", tree)
        marker = tree / "parked-marker.txt"
        marker.write_text("this file belongs to the parked run\n", encoding="utf-8")

        driver(fleet, tree)

        assert tree.is_dir(), f"{key} removed the parked run's worktree"
        assert marker.is_file(), f"{key} removed the parked run's worktree"


def test_no_removal_path_decides_liveness_from_the_phase_gated_claim() -> None:
    """Every removal path reads the wide claim, none the phase-gated one.

    The enumeration discovers the paths by parsing the package, so a removal
    path added later is judged by the same rule as the ones here. The rule
    covers the sanctioned release door too: the door removes a tree its own
    record names, but a peer parked on an external wait may name the same
    tree, so that liveness question is answered by the wide claim as well.
    """
    paths = _removal_paths()
    assert paths, "the removal-command scan found nothing; its subject is gone"
    assert set(paths) == set(REMOVAL_DRIVERS), (
        "the set of removal paths changed; every one of them needs a direct "
        "driver in REMOVAL_DRIVERS so it is called against a parked tree"
    )
    for key, path in sorted(paths.items()):
        assert path.reads_wide, (
            f"{key} removes a worktree but reads neither claim; no live "
            "pointer's tree is protected without the wide claim"
        )
        assert not path.reads_phase_gated, (
            f"{key} decides liveness through the phase-gated "
            f"{PHASE_GATED_CLAIM}: a parked run's pointer reads as "
            "terminal-looking there, so its tree reads as reclaimable"
        )


def test_the_scan_flags_a_phase_gated_reclaim_path() -> None:
    """The scanner sees what is present: a planted phase-gated path is flagged."""
    source = (
        "def _scratch_reclaim(repo, path):\n"
        "    if _live_worktree_claims().get(path):\n"
        "        raise CrewError('claimed')\n"
        "    _git(repo, 'worktree', 'remove', str(path))\n"
    )
    found = _scan_source(source, "reckon/scratch.py")
    assert list(found) == ["reckon/scratch.py::_scratch_reclaim"]
    planted = found["reckon/scratch.py::_scratch_reclaim"]
    assert planted.reads_phase_gated
    assert not planted.reads_wide
