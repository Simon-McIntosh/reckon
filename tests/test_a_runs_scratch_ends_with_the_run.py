"""A run's scratch ends with the run, never with its age.

The node-local temp tree is what fills the disk, and a run's own scratch
directory beneath a reckon-owned root is removed by promotion and discard. Two
gaps remain, and this file closes both. A run that left a tree at an explicit
``/tmp/<name>`` path its brief named is not reachable from the run's scratch, so
the composed worker contract now sends a temporary tree to ``$TMPDIR`` where
promotion already reclaims it. And a scratch tree that no terminal record
accounts for is not garbage: a complete-but-unpromoted run is revisited hours
after its worker exits, so ``crew gc --scratch`` decides by the run's records —
a live pointer forbids removal and a ledger row or a discard marker licenses it
— and never by the directory's age.

Five properties:

* a promoted run's scratch is removed by ``gc --scratch``;
* a complete-but-unpromoted run's scratch, older than any age bound, is kept
  and reported live;
* a discarded run's scratch is removed;
* a tree no run can be attributed to is reported and kept, even under
  ``--apply``;
* the composed worker contract names ``$TMPDIR`` for temporary trees.

Everything is synthesised under ``tmp_path`` and the scratch root is redirected
by ``RECKON_WORKER_SCRATCH_ROOT``, so no case reads or writes the host's real
temp directory and no gc runs with ``--apply`` against the real crew home.

The declared mutation: make gc decide by age. Under it the case keeping a
complete-but-unpromoted run's scratch fails.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, ledger
from reckon.crew import promotion, routing
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from reckon.crew.runs import _write_json, pointer_path

# `reckon.crew.dispatch` is shadowed by the dispatch function the package
# re-exports, so the module is reached by importlib rather than by attribute.
dispatch = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
PLAN = "plan-a"

# The declared mutation, applied by the case that must fail under it: gc keys
# its disposition on the directory's age rather than on the run's own records.
AGE_CONTROL_MUTATION = (
    "in a scratch tree, make gc decide by age; the case keeping a "
    "complete-but-unpromoted run's scratch then fails"
)
AGE_CONTROL = os.environ.get("RECKON_SCRATCH_GC_NEGATIVE") == "1"
AGE_BOUND_SECONDS = 3600


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


def _scratch(run_id: str) -> Path:
    """A run's scratch directory holding a file, as a real worker would leave."""
    directory = dispatch.ensure_worker_scratch(run_id)
    (directory / "worker-temp.txt").write_text("scratch\n", encoding="utf-8")
    return directory


def _age(path: Path, seconds: int) -> None:
    """Push the directory's own mtime back, so an age bound would see it old."""
    stale = path.stat().st_mtime - seconds
    os.utime(path, (stale, stale))


def _live_pointer(run_id: str, repository: Path) -> None:
    """A state a run leaves behind before promotion: a live pointer, no row."""
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "phase": "working",
            "role": "implement",
            "created_at": "2026-09-28T14:00:00Z",
            "scratch": str(dispatch.worker_scratch_dir(run_id)),
            "manifest_path": "/nonexistent/manifest.md",
            "pid": None,
            "pid_start_time": None,
        },
    )


def _ledger_row(run_id: str, repository: Path) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(run_id=run_id, plan=PLAN, gate="passed", node="node-a"),
        root=repository,
    )


def _discard_marker(run_id: str) -> None:
    _write_json(
        promotion.discard_record_path(run_id),
        {
            "run_id": run_id,
            "discarded_at": "2026-09-28T15:00:00Z",
            "phase": "working",
            "node": "node-a",
        },
    )


def _run_gc(
    repository: Path, scratch_root: Path, tmp_path: Path, *, apply: bool
) -> dict:
    return routing.garbage_collect_scratch(
        repo=repository,
        project=None,
        apply=apply,
        scratch_root=scratch_root,
        tmp_root=tmp_path,
    )


def _entry(report: dict, path: Path) -> dict:
    for item in report["directories"]:
        if item["path"] == str(path.resolve()):
            return item
    raise AssertionError(f"no scratch entry for {path}")


def _age_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation: decide by age, ignoring the run's own records."""

    def _decide(path, *, live_ids, ledgered, discard_recorded):
        try:
            stale = time.time() - path.stat().st_mtime
        except OSError:
            return routing.SCRATCH_UNATTRIBUTED
        if stale >= AGE_BOUND_SECONDS:
            return routing.SCRATCH_TERMINAL
        return routing.SCRATCH_UNATTRIBUTED

    monkeypatch.setattr(routing, "_scratch_disposition", _decide)


def test_a_dry_run_keeps_it_and_apply_removes_a_promoted_runs_scratch(
    scratch_root: Path, repository: Path, tmp_path: Path
) -> None:
    """Case 1: a promoted run's scratch goes, and only under --apply."""
    run_id = "r-20260928T140000000000-scratch-promoted"
    scratch = _scratch(run_id)
    _ledger_row(run_id, repository)
    _age(scratch, 10 * AGE_BOUND_SECONDS)

    dry = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repository), "--scratch"]
    )
    assert dry.exit_code == 0, dry.output
    assert scratch.is_dir(), "a dry run must not remove anything"
    assert "would remove" in dry.output
    assert str(scratch) in dry.output

    report = _run_gc(repository, scratch_root, tmp_path, apply=False)
    assert _entry(report, scratch)["state"] == routing.SCRATCH_TERMINAL

    applied = CliRunner().invoke(
        cli.main,
        ["crew", "gc", "--repo", str(repository), "--scratch", "--apply"],
    )
    assert applied.exit_code == 0, applied.output
    assert f"removing worker scratch directory {scratch}" in applied.output
    assert not scratch.exists(), "a promoted run's scratch must be gone"


def test_a_complete_but_unpromoted_runs_scratch_is_kept(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 2: a live pointer keeps the tree however old it is.

    The run is complete but unpromoted and its worker is long gone, so the tree
    is older than any age bound. It is revisited after that age, so the record
    and not the clock decides, and the disposition reports it live.
    """
    if AGE_CONTROL:
        _age_control(monkeypatch)
    run_id = "r-20260928T140100000000-scratch-unpromoted"
    scratch = _scratch(run_id)
    _live_pointer(run_id, repository)
    _age(scratch, 10 * AGE_BOUND_SECONDS)

    report = _run_gc(repository, scratch_root, tmp_path, apply=True)

    assert scratch.is_dir(), "an unpromoted run's scratch must be kept"
    assert (scratch / "worker-temp.txt").is_file()
    assert _entry(report, scratch)["state"] == routing.SCRATCH_LIVE


def test_a_discarded_runs_scratch_is_removed(
    scratch_root: Path, repository: Path, tmp_path: Path
) -> None:
    """Case 3: a discard marker is a terminal record, so the tree goes."""
    run_id = "r-20260928T140200000000-scratch-discarded"
    scratch = _scratch(run_id)
    _discard_marker(run_id)

    report = _run_gc(repository, scratch_root, tmp_path, apply=True)

    assert not scratch.exists(), "a discarded run's scratch must be gone"
    assert str(scratch.resolve()) in report["removed"]
    assert _entry(report, scratch)["state"] == routing.SCRATCH_TERMINAL


def test_an_unattributed_tree_is_reported_and_kept(
    scratch_root: Path, repository: Path, tmp_path: Path
) -> None:
    """Case 4: a tree no run can be attributed to survives even --apply."""
    stray = tmp_path / "stray-worker-tree"
    stray.mkdir()
    (stray / "arm.tar").write_text("archive\n", encoding="utf-8")

    report = _run_gc(repository, scratch_root, tmp_path, apply=True)

    assert stray.is_dir(), "an unattributed tree must never be removed"
    assert (stray / "arm.tar").is_file()
    paths = {item["path"]: item for item in report["unattributed"]}
    assert str(stray.resolve()) in paths
    assert paths[str(stray.resolve())]["bytes"] > 0
    assert paths[str(stray.resolve())]["ctime"]


def test_the_worker_contract_names_tmpdir_for_temporary_trees() -> None:
    """Case 5: the composed prompt sends a temporary tree to $TMPDIR."""
    node = TaskNode(
        id="scratch-contract-node",
        goal="a temporary tree lives under the run's own scratch",
        plan=PLAN,
        section="",
        role="implement",
        done_when="the composed contract names $TMPDIR for a temporary tree",
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )
    prompt = compose_prompt(
        node=node,
        project=PROJECT,
        worktree="/repo/worktrees/scratch-node",
        working_directory="/repo/worktrees/scratch-node",
        manifest_path="/state/runs/scratch-node/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )
    assert "$TMPDIR" in prompt
    assert "WORKTREE AND PARALLEL-SAFETY RULES" in prompt


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(AGE_CONTROL_MUTATION)
    raise SystemExit(1)
