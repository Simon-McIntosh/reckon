"""A reclaimed run's review keys on the head its own record names.

Measured on 2026-10-02: a run whose worktree an earlier refused promotion had
removed was reviewed five times in half an hour, with a sixth running. The
manifest cited its commits by nine-character ids, the pointer carried no head,
and neither reading resolved a revision, so the review fell through to the
shared checkout's HEAD. Each review's own promotion commit moved that HEAD, the
next sweep found the stored review stale, and the loop dispatched another.

Two readings are asserted here. An abbreviated ``commits:`` entry resolves to
this run's commit through the run's repository, which shares the object store
the reclaimed worktree wrote into. And a reclaimed run whose record names no
resolvable head composes no review and records the missing head as the reason,
because a review of the checkout's HEAD is a review of code this run never
carried. The repository keeps a later commit at its HEAD in both cases, so the
empty reading is a refusal and not an absent repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

PROJECT = "reclaimed-fixture"
SESSION = "coordinator-fixture"
SOURCE_RUN = "r-reclaimed-source"

REAL_HOME = Path.home() / ".config" / "reckon"

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "service": False,
            "sandbox": "worktree-full",
            "session_reuse": False,
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class _Fleet:
    """One throwaway project: a temporary repository and the runs in it."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.home = tmp_path / "config"
        self.repo = tmp_path / "repo"
        self.launches: list = []
        self._monkeypatch = monkeypatch
        self.home.mkdir(parents=True)
        self._seed()
        monkeypatch.setenv("RECKON_HOME", str(self.home))
        (self.home / "mounts.json").write_text(
            json.dumps({PROJECT: str(self.repo / "docs")}), encoding="utf-8"
        )

    def _seed(self) -> None:
        (self.repo / "docs" / "plans").mkdir(parents=True)
        (self.repo / "docs" / "plans" / "fixture.html").write_text(
            f'<meta name="docs-project" content="{PROJECT}">'
            '<meta name="reckon-type" content="plan">'
            '<meta name="plan-slug" content="fixture">'
            '<h2 id="s1">A reclaimed run keys its review on its own head</h2>',
            encoding="utf-8",
        )
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        for arguments in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "worker@example.invalid"),
            ("config", "user.name", "Worker"),
            ("add", "seed.txt", "docs/plans/fixture.html"),
            ("commit", "-q", "-m", "chore: seed"),
        ):
            _git(self.repo, *arguments)

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")

    def commit(self, name: str, *, suffix: str = ".txt") -> str:
        path = f"{name}{suffix}"
        (self.repo / path).write_text(f"{name}\n", encoding="utf-8")
        _git(self.repo, "add", path)
        _git(self.repo, "commit", "-q", "-m", f"chore: add {name}")
        return self.head()

    def reclaimed_record(self, *, commits: list[str], head: str = "") -> dict:
        """A completed run whose worktree is gone: manifest, pointer, neither head."""
        manifest = self.home / "manifests" / f"{SOURCE_RUN}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: source-node\nstatus: complete\ncommits: [{', '.join(commits)}]\n",
            encoding="utf-8",
        )
        record = {
            "run_id": SOURCE_RUN,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "repo": str(self.repo),
            "worktree": str(self.repo.parent / "worktrees" / "reclaimed"),
            "backend": "alpha",
            "launch": "cli",
            "argv": ["codex"],
            "phase": "starting",
            "node": {"id": "source-node", "plan": "fixture", "section": "s1"},
            "manifest_path": str(manifest),
        }
        if head:
            record["head"] = head
        runs._write_json(runs.pointer_path(SOURCE_RUN), record)
        return record

    def sweep(self) -> dict:
        def launcher(plan, *, log_path, stderr_path, prompt_path):
            self.launches.append(plan)
            return os.getpid()

        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            return recovery.dispatch_awaiting_reviews(
                project=PROJECT, config=CONFIG, launcher=launcher
            )


@pytest.fixture()
def fleets(tmp_path, monkeypatch):
    return lambda: _Fleet(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def real_config_home_untouched():
    """No fixture write reaches the real configuration home."""
    yield
    reviews = REAL_HOME / "crew" / "reviews" / PROJECT
    assert not reviews.exists(), f"fixture review store leaked to {reviews}"
    live = REAL_HOME / "crew" / "live"
    leaked = [
        str(path)
        for path in (live.glob("*.json") if live.is_dir() else ())
        if PROJECT in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert not leaked, f"fixture pointers leaked to the real live directory: {leaked}"


def test_an_abbreviated_commit_entry_keys_the_review_on_the_run_head(fleets) -> None:
    """The nine-character citation resolves to this run's commit, not the HEAD.

    The repository is left holding a later commit at its HEAD, which is the
    revision the old reading fell through to: the assertion that the two differ
    is what shows the resolution went through the citation and not the checkout.
    """
    fleet = fleets()
    run_head = fleet.commit("landing", suffix=".py")
    later_head = fleet.commit("later", suffix=".py")
    assert run_head != later_head
    assert fleet.head() == later_head

    record = fleet.reclaimed_record(commits=[run_head[:9]])

    resolved = recovery._run_head_for_review(record)
    assert resolved == run_head
    assert resolved != later_head


def test_a_reclaimed_run_with_no_resolvable_head_composes_no_review(fleets) -> None:
    """No resolvable head: nothing dispatches, and the missing head is named.

    The repository keeps a commit at its HEAD, so the empty reading is a
    refusal of the shared checkout's head rather than the absence of one.
    """
    fleet = fleets()
    repository_head = fleet.head()
    assert repository_head

    record = fleet.reclaimed_record(commits=[])

    assert recovery._review_head_and_tree(record) == ("", None)

    result = fleet.sweep()

    assert result["dispatched"] == []
    assert fleet.launches == []
    rows = [row for row in result["reports"] if row.get("run_id") == SOURCE_RUN]
    assert len(rows) == 1, result["reports"]
    report = rows[0]
    assert report.get("dispatched") is False
    assert "no resolvable head" in str(report.get("reason"))
    recorded = runs.read_pointer(SOURCE_RUN).get("review_dispatch") or {}
    assert recorded.get("status") == "refused"
    assert "no resolvable head" in str(recorded.get("reason"))
