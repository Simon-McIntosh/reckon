"""A promotion returns without waiting for its worktree copy's deletion.

A released tree is vacated in one rename and deregistered, and then its copy is
deleted. That deletion is the slowest step of a promotion on a shared
filesystem, and it runs while the promotion still holds its run's lock, so a
serial review sweep waits on it between reviews. The copy is left in the trash
root instead, which the gc pass sweeps and prints; every other release path
still deletes inline.

These tests drive real ``crew complete --gate not-run`` promotions over a
temporary crew home, checkout and linked worktrees, and slow the deletion with
an injected 30 s sleep rather than waiting on a live filesystem.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger
from reckon import cli as cli_module
from reckon.crew import routing as routing_module
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
SESSION = "promotion-copy-session"
GATE_COMMAND = f"{shlex.quote(sys.executable)} -c pass"
INJECTED_DELAY_SECONDS = 30.0
BUDGET_SECONDS = 5.0


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A committed checkout a promotion can land its record into."""
    root = tmp_path / "repo"
    root.mkdir()
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "docs", "seed.txt")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    ledger.write(
        PROJECT,
        {"members": [], "runs": [], "holds": []},
        0,
        root=root,
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _worktree(repository: Path, name: str) -> Path:
    """A clean linked worktree at the same head as the repository."""
    tree = repository.parent / "worktrees" / SESSION / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def _trash_entries(repository: Path) -> list[Path]:
    root = (
        routing_module._workspace_roots(repository)[0]
        / routing_module.WORKTREE_TRASH_DIRNAME
    )
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _write_completed_run(
    repository: Path, tree: Path, tmp_path: Path, run_id: str
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "changed_paths: none (the sole deliverable is the report)\n"
        f"tests: {GATE_COMMAND}\n",
        encoding="utf-8",
    )
    run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-11-12T18:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


def _promote(repository: Path, run_id: str):
    """Run one ``crew complete --gate not-run`` and return the click result."""
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "not-run",
            "--outcome",
            "the fixture exercises a promotion's worktree-copy release",
            "--no-commit",
            "the sole deliverable is the report",
            "--checkout-path",
            str(repository),
        ],
    )


def _slow_copy_deletion(seconds: float):
    """Substitute the copy's deletion with one that takes ``seconds``."""

    def _sleeping_delete(trash: Path) -> None:
        assert trash.is_dir(), "the deletion was handed a copy that is not there"
        time.sleep(seconds)

    return _sleeping_delete


def _wait_for_trash(repository: Path, timeout: float = 20.0) -> list[Path]:
    """Wait until the first promotion has moved its copy aside."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entries = _trash_entries(repository)
        if entries:
            return entries
        time.sleep(0.05)
    raise AssertionError("the promotion never moved its worktree copy aside")


def test_a_promotion_returns_before_its_copy_is_deleted(
    repository: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A slowed deletion costs the promotion almost none of its own time.

    The same promotion runs twice: once with the copy's deletion left alone, and
    once with it slowed by 30 s. The second must return within a few seconds of
    the first, and the copy it moved aside must still be there to sweep.
    """
    plain_tree = _worktree(repository, "plain-release")
    plain_run = "r-20261112T180000000000-plain-release"
    _write_completed_run(repository, plain_tree, tmp_path, plain_run)

    started = time.monotonic()
    plain = _promote(repository, plain_run)
    plain_seconds = time.monotonic() - started
    assert plain.exit_code == 0, plain.output
    assert not plain_tree.exists(), "the released tree stayed at its path"

    slowed_tree = _worktree(repository, "slowed-release")
    slowed_run = "r-20261112T180000000001-slowed-release"
    _write_completed_run(repository, slowed_tree, tmp_path, slowed_run)

    started = time.monotonic()
    with mock.patch.object(
        routing_module,
        "_delete_worktree_copy",
        _slow_copy_deletion(INJECTED_DELAY_SECONDS),
    ):
        slowed = _promote(repository, slowed_run)
    slowed_seconds = time.monotonic() - started

    assert slowed.exit_code == 0, slowed.output
    assert not slowed_tree.exists(), "the released tree stayed at its path"
    assert slowed_seconds - plain_seconds < BUDGET_SECONDS, (
        f"the promotion waited on its copy's deletion: {slowed_seconds:.1f}s "
        f"against {plain_seconds:.1f}s without the injected delay"
    )

    # The slowed promotion's copy is left for the trash sweep, not deleted.
    copies = [
        path for path in _trash_entries(repository) if "slowed-release" in path.name
    ]
    assert copies, _trash_entries(repository)

    # A copy an hour old does not survive a pass of the sweep, which prints it.
    two_hours_ago = time.time() - 7200
    for copy in copies:
        os.utime(copy, (two_hours_ago, two_hours_ago))

    report = routing_module.garbage_collect(
        repo=repository, project=PROJECT, apply=True
    )
    assert report["worktree_trash_removed"] >= len(copies), report["worktree_trash"]
    assert all(not copy.exists() for copy in copies)
    printed = capsys.readouterr().out
    for copy in copies:
        assert str(copy) in printed, printed


def test_a_second_promotion_is_not_held_by_the_firsts_copy_deletion(
    repository: Path, tmp_path: Path
) -> None:
    """A promotion started while a copy is being deleted does not wait for it.

    The first promotion slows its copy's deletion to 30 s. Once it has moved the
    copy aside, a second promotion of the same run starts: it needs the run's
    promotion lock, which the first holds for the whole of its release. It must
    reach the lock within a few seconds rather than after the deletion.
    """
    tree = _worktree(repository, "held-lock")
    run_id = "r-20261112T180000000002-held-lock"
    _write_completed_run(repository, tree, tmp_path, run_id)

    outcome: dict[str, object] = {}

    def _first() -> None:
        outcome["result"] = _promote(repository, run_id)

    with mock.patch.object(
        routing_module,
        "_delete_worktree_copy",
        _slow_copy_deletion(INJECTED_DELAY_SECONDS),
    ):
        thread = threading.Thread(target=_first)
        thread.start()
        try:
            _wait_for_trash(repository)
            started = time.monotonic()
            second = _promote(repository, run_id)
            waited = time.monotonic() - started
        finally:
            thread.join(timeout=INJECTED_DELAY_SECONDS + 30)

    assert isinstance(outcome.get("result"), object)
    assert waited < BUDGET_SECONDS, (
        f"the second promotion waited {waited:.1f}s for the first's deletion"
    )
    assert second.exit_code != 0 or second.output, (
        "the second promotion reported nothing"
    )
