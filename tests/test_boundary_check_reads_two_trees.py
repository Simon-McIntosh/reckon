"""A fenced run's boundary check reads its own worktree and the main checkout.

Every dispatch records a snapshot of every worktree registered in the
repository, and every promotion takes it again, at one ``git status`` per tree.
A fenced worker cannot write outside its own worktree, so a boundary check over
the other trees looks for a write the operating system already refused. These
tests drive a temporary repository with fifty registered worktrees and a ``git``
shim on ``PATH`` that counts ``status`` calls, and show that a fenced run reads
two trees while an unfenced one reads the whole registry.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path, run_dir

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "sample"
REGISTERED_WORKTREES = 50
FULL_SCAN = REGISTERED_WORKTREES + 1


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def worktrees(repository: Path, tmp_path: Path) -> list[Path]:
    """Fifty registered worktrees; the first is the run's own worktree."""
    trees_root = tmp_path / "trees"
    trees_root.mkdir()
    created: list[Path] = []
    for index in range(REGISTERED_WORKTREES):
        tree = trees_root / f"tree{index:02d}"
        _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
        created.append(tree)
    return created


def _real_git() -> str:
    """The first non-shim ``git`` on PATH.

    The workstation puts a reckon ``git`` shim on PATH, so the ordinary
    ``shutil.which`` result is a wrapper that itself runs ``git status`` and
    would count its own calls. Skip any candidate carrying the reckon token.
    """
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "git"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            head = candidate.read_text(errors="ignore")[:200]
        except OSError:
            continue
        if "reckon-shim" in head or "reckon/shim_lookup" in head:
            continue
        return str(candidate)
    raise AssertionError("no non-shim git on PATH")


@pytest.fixture()
def status_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a ``git`` shim that appends the cwd of every ``status`` call.

    The real binary is captured first so the shim can forward everything else.
    """
    real_git = _real_git()
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    script = shim_dir / "git"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "status" ]; then printf \'%s\\n\' "$PWD" >> "$RECKON_STATUS_LOG"; fi\n'
        f'exec {real_git} "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    log = tmp_path / "status.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("RECKON_STATUS_LOG", str(log))
    return log


def _status_calls(log: Path) -> list[str]:
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _reset(log: Path) -> None:
    log.write_text("", encoding="utf-8")


def _run_record(
    repository: Path,
    run_tree: Path,
    run_id: str,
    base: str,
    *,
    fenced: bool | None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(run_tree),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-09-29T12:00:00Z",
        "node": {
            "id": "boundary-check",
            "plan": "fixture",
            "section": "s1",
            "time_budget": "25m",
            "write_paths": ["allowed.txt"],
        },
    }
    if fenced is not None:
        record["fenced"] = fenced
    return record


def _write_run_snapshot(
    repository: Path,
    run_tree: Path,
    run_id: str,
    *,
    fenced: bool,
) -> Path:
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    dispatch_module._write_boundary_tree_snapshot(
        directory,
        repository,
        worktree=run_tree,
        fenced=fenced,
    )
    return directory / dispatch_module.TREE_SNAPSHOT_NAME


def test_a_fenced_snapshot_holds_two_trees_and_an_unfenced_one_the_registry(
    repository: Path, worktrees: list[Path], tmp_path: Path, status_log: Path
) -> None:
    run_tree = worktrees[0]
    directory = tmp_path / "fenced-run"
    directory.mkdir()

    _reset(status_log)
    fenced = dispatch_module._write_boundary_tree_snapshot(
        directory, repository, worktree=run_tree, fenced=True
    )
    fenced_calls = _status_calls(status_log)

    _reset(status_log)
    unfenced = dispatch_module._write_boundary_tree_snapshot(
        directory, repository, worktree=run_tree, fenced=False
    )
    unfenced_calls = _status_calls(status_log)

    assert len(fenced["trees"]) == 2
    assert {Path(t["path"]).resolve() for t in fenced["trees"]} == {
        repository.resolve(),
        run_tree.resolve(),
    }
    assert len(fenced_calls) == 2

    assert len(unfenced["trees"]) == FULL_SCAN
    assert len(unfenced_calls) == FULL_SCAN


def test_a_fenced_promotion_makes_two_status_calls_and_an_unfenced_one_fifty_one(
    repository: Path, worktrees: list[Path], status_log: Path
) -> None:
    run_tree = worktrees[0]
    base = _git(repository, "rev-parse", "HEAD")

    fenced_id = "r-fenced-boundary"
    _write_run_snapshot(repository, run_tree, fenced_id, fenced=True)
    fenced_record = _run_record(repository, run_tree, fenced_id, base, fenced=True)
    _reset(status_log)
    assert (
        promotion._repository_tree_boundary_violations(fenced_id, fenced_record) == []
    )
    fenced_calls = _status_calls(status_log)

    unfenced_id = "r-unfenced-boundary"
    _write_run_snapshot(repository, run_tree, unfenced_id, fenced=False)
    unfenced_record = _run_record(repository, run_tree, unfenced_id, base, fenced=False)
    _reset(status_log)
    assert (
        promotion._repository_tree_boundary_violations(unfenced_id, unfenced_record)
        == []
    )
    unfenced_calls = _status_calls(status_log)

    assert len(fenced_calls) == 2
    assert len(unfenced_calls) == FULL_SCAN


def test_a_record_without_the_fence_field_keeps_the_full_scan(
    repository: Path, worktrees: list[Path], status_log: Path
) -> None:
    """A run recorded before the field existed reads every registered tree."""
    run_tree = worktrees[0]
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-legacy-boundary"
    _write_run_snapshot(repository, run_tree, run_id, fenced=False)
    record = _run_record(repository, run_tree, run_id, base, fenced=None)

    _reset(status_log)
    assert promotion._repository_tree_boundary_violations(run_id, record) == []
    calls = _status_calls(status_log)

    assert len(calls) == FULL_SCAN


def test_a_fenced_run_reads_no_peer_worktree_through_the_complete_entry_point(
    repository: Path, worktrees: list[Path], status_log: Path
) -> None:
    """The promotion the entry point runs never opens a peer worktree."""
    run_tree = worktrees[0]
    peers = {tree.resolve() for tree in worktrees[1:]}
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-fenced-complete"
    directory = _write_run_snapshot(repository, run_tree, run_id, fenced=True)
    assert directory.is_file()
    _write_json(
        pointer_path(run_id),
        _run_record(repository, run_tree, run_id, base, fenced=True),
    )
    (run_tree / "allowed.txt").write_text("seed\nwork\n", encoding="utf-8")
    _git(run_tree, "add", "allowed.txt")
    _git(run_tree, "commit", "-q", "-m", "test: worker edit")
    commit = _git(run_tree, "rev-parse", "HEAD")

    _reset(status_log)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    peer_calls = [call for call in _status_calls(status_log) if Path(call) in peers]

    assert peer_calls == []


def test_the_counter_sees_an_unfenced_complete_open_peer_worktrees(
    repository: Path, worktrees: list[Path], status_log: Path
) -> None:
    """Positive control: the same shim counts an unfenced run's peer reads.

    Without it the empty list above is indistinguishable from a shim that
    counts nothing.
    """
    run_tree = worktrees[0]
    peers = {tree.resolve() for tree in worktrees[1:]}
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-unfenced-complete"
    _write_run_snapshot(repository, run_tree, run_id, fenced=False)
    _write_json(
        pointer_path(run_id),
        _run_record(repository, run_tree, run_id, base, fenced=False),
    )
    (run_tree / "allowed.txt").write_text("seed\nwork\n", encoding="utf-8")
    _git(run_tree, "add", "allowed.txt")
    _git(run_tree, "commit", "-q", "-m", "test: worker edit")
    commit = _git(run_tree, "rev-parse", "HEAD")

    _reset(status_log)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    peer_calls = [call for call in _status_calls(status_log) if Path(call) in peers]

    assert len(peer_calls) == REGISTERED_WORKTREES - 1


def test_a_fenced_run_is_still_refused_when_it_wrote_the_main_checkout(
    repository: Path, worktrees: list[Path]
) -> None:
    run_tree = worktrees[0]
    base = _git(repository, "rev-parse", "HEAD")
    run_id = "r-fenced-main-write"
    _write_run_snapshot(repository, run_tree, run_id, fenced=True)
    _write_json(
        pointer_path(run_id),
        _run_record(repository, run_tree, run_id, base, fenced=True),
    )
    (run_tree / "allowed.txt").write_text("seed\nwork\n", encoding="utf-8")
    _git(run_tree, "add", "allowed.txt")
    _git(run_tree, "commit", "-q", "-m", "test: worker edit")
    commit = _git(run_tree, "rev-parse", "HEAD")
    (repository / "allowed.txt").write_text("stray\n", encoding="utf-8")

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "allowed.txt" in message
    assert f"main checkout {repository}" in message
    assert pointer_path(run_id).is_file()
