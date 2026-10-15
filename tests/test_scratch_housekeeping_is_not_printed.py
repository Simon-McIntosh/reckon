"""Scratch housekeeping names its removals on the result, never on the pane.

Promotion and discard run inside a follower or a producer, whose stdout is a
session pane. When that step printed ``removing worker scratch directory …`` and
``removed worker scratch directory …`` the pane filled with housekeeping a reader
does not need, while the removal was already recorded on the release result. The
same held for ``crew gc --scratch``, whose report carries every directory and its
disposition.

Three properties:

* a promotion removes the run's scratch and names the path and size on its
  result, and prints nothing about scratch;
* ``crew gc --scratch --apply`` names the removed path in its emitted JSON, and
  no housekeeping line reaches stdout outside it;
* a ``crew gc --scratch`` dry run names the directory it would remove in its
  emitted JSON, and prints nothing about scratch either.

Everything is synthesised under ``tmp_path`` — the scratch root is redirected by
``RECKON_WORKER_SCRATCH_ROOT`` and the crew home by ``RECKON_HOME`` — so no case
reads or writes the host's real temp directory or crew home.

Two declared mutations, each applied by an environment flag and each logged
binary. Restoring the print in ``remove_worker_scratch`` turns the promotion case
red; restoring the print in ``garbage_collect_scratch`` turns the ``crew gc
--apply`` case red.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, ledger
from reckon.crew import promotion_release, routing
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git

# ``reckon.crew.dispatch`` is shadowed by the dispatch function the package
# re-exports, so the module is reached by importlib rather than by attribute.
dispatch = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
PLAN = "plan-a"

# The first declared mutation: restore the print in ``remove_worker_scratch``.
PROMOTION_PRINT_CONTROL_MUTATION = (
    "restore the print in remove_worker_scratch; the in-process promotion case "
    "then finds a housekeeping line on stdout and fails"
)
PROMOTION_PRINT_CONTROL = os.environ.get("RECKON_SCRATCH_PRINT_PROMOTION") == "1"

# The second declared mutation: restore the print in ``garbage_collect_scratch``.
GC_PRINT_CONTROL_MUTATION = (
    "restore the print in garbage_collect_scratch; the crew gc --apply case "
    "then finds a housekeeping line outside the emitted JSON and fails"
)
GC_PRINT_CONTROL = os.environ.get("RECKON_SCRATCH_PRINT_GC") == "1"

# Each string the removed ``print`` calls emitted. A line carrying any of them
# is housekeeping that reached stdout rather than the result.
_HOUSEKEEPING_MARKERS = (
    "removing worker scratch directory",
    "removed worker scratch directory",
    "would remove worker scratch directory",
    "warning: run scratch for",
)


def _housekeeping_lines(text: str) -> list[str]:
    """The lines of ``text`` that are scratch housekeeping rather than a result."""
    return [
        line
        for line in text.splitlines()
        if any(marker in line for marker in _HOUSEKEEPING_MARKERS)
    ]


@pytest.fixture()
def scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch root under the test's temp directory, never the host's."""
    root = tmp_path / "scratch"
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(root))
    return root


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real repository with its crew directories under a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt", "docs")
    _git(repo, "commit", "-q", "-m", "seed repository")
    return repo


def _worktree(repository: Path, tmp_path: Path, name: str) -> Path:
    worktree = tmp_path / "worktrees" / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
    )
    return worktree


def _pointer(run_id: str, *, repository: Path, worktree: Path) -> None:
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "created_at": "2026-10-15T06:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "scratch": str(dispatch.worker_scratch_dir(run_id)),
            "manifest_path": "/nonexistent/manifest.md",
            "pid": None,
            "pid_start_time": None,
            "node": {
                "id": run_id.rsplit("-", 1)[-1],
                "plan": PLAN,
                "section": "24",
                "time_budget": "40m",
                "write_paths": [],
            },
        },
    )


def _plant_scratch(run_id: str) -> Path:
    """A run's scratch directory holding a file, as a real worker would leave."""
    directory = dispatch.ensure_worker_scratch(run_id)
    (directory / "worker-temp.txt").write_text("scratch\n", encoding="utf-8")
    return directory


def _ledger_row(run_id: str, repository: Path) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id=run_id, plan=PLAN, gate="passed", node="node-a"),
        root=repository,
    )


def _restore_promotion_print(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation: print each removed path as the release runs."""
    real = promotion_release.remove_worker_scratch

    def noisy(run_id: str, **kwargs: object) -> dict:
        result = real(run_id, **kwargs)
        for item in result.get("scratch_removed_paths", []):
            print(f"removed worker scratch directory {item['path']}")
        return result

    monkeypatch.setattr(promotion_release, "remove_worker_scratch", noisy)


def _restore_gc_print(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation: print what each scratch sweep removes or would remove."""
    real = routing.garbage_collect_scratch

    def noisy(**kwargs: object) -> dict:
        report = real(**kwargs)
        for item in report["directories"]:
            if item["state"] == routing.SCRATCH_TERMINAL:
                verb = "removing" if item.get("removed") else "would remove"
                print(
                    f"{verb} worker scratch directory {item['path']} "
                    f"({item['bytes']} bytes)"
                )
        return report

    monkeypatch.setattr(routing, "garbage_collect_scratch", noisy)


def test_a_promotion_names_the_removal_without_printing(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A promoted run's scratch is gone, named on the result, and not printed."""
    if PROMOTION_PRINT_CONTROL:
        _restore_promotion_print(monkeypatch)
    run_id = "r-20261015T060000000000-scratch-promote"
    worktree = _worktree(repository, tmp_path, "promote")
    _pointer(run_id, repository=repository, worktree=worktree)
    scratch = _plant_scratch(run_id)
    assert scratch.is_dir()

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="the scratch removal is named on the result",
        completed_at="2026-10-15T06:10:00Z",
        root=repository,
    )

    captured = capsys.readouterr().out
    assert _housekeeping_lines(captured) == [], captured
    release = promoted["release"]
    assert release["scratch_removed"] is True
    removed = {item["path"]: item["bytes"] for item in release["scratch_removed_paths"]}
    assert removed.get(str(scratch), 0) > 0
    assert not scratch.exists(), "the run's scratch directory must be gone"


def test_the_gc_apply_names_removals_in_json_not_stdout(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``crew gc --apply`` removes the tree, names it in JSON, and prints nothing."""
    if GC_PRINT_CONTROL:
        _restore_gc_print(monkeypatch)
    run_id = "r-20261015T060100000000-scratch-gc-apply"
    scratch = _plant_scratch(run_id)
    _ledger_row(run_id, repository)

    result = CliRunner().invoke(
        cli.main,
        ["crew", "gc", "--repo", str(repository), "--scratch", "--apply"],
    )
    assert result.exit_code == 0, result.output
    assert _housekeeping_lines(result.output) == [], result.output
    payload = json.loads(result.output)
    assert str(scratch.resolve()) in payload["removed"]
    assert not scratch.exists(), "a promoted run's scratch must be gone"


def test_a_gc_dry_run_names_the_directory_in_json_not_stdout(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run names the directory it would remove in JSON, printing nothing."""
    if GC_PRINT_CONTROL:
        _restore_gc_print(monkeypatch)
    run_id = "r-20261015T060200000000-scratch-gc-dry"
    scratch = _plant_scratch(run_id)
    _ledger_row(run_id, repository)

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repository), "--scratch"]
    )
    assert result.exit_code == 0, result.output
    assert _housekeeping_lines(result.output) == [], result.output
    # The would-remove directory is named in the emitted JSON, not printed.
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    entries = {item["path"]: item for item in payload["directories"]}
    assert str(scratch.resolve()) in entries
    assert scratch.is_dir(), "a dry run must not remove anything"


if __name__ == "__main__":  # pragma: no cover - names the declared mutations
    print(PROMOTION_PRINT_CONTROL_MUTATION)
    print(GC_PRINT_CONTROL_MUTATION)
    raise SystemExit(1)
