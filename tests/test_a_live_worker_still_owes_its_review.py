"""A delivered run whose worker is still alive still owes its tier's review.

A terminal manifest is what a promotion proceeds on, and the release path
exists so that a promotion may proceed while the run's recorded process is
still alive. The classifier reads that pair as ``running``: a terminal manifest
plus a live process defers the run's outcome rather than calling it delivered.
Promotion must not read that deferral as "no review owed", because the delivery
is the one it would be promoting had the worker stopped, so the gate reads the
obligation from the delivery. A runtime-source delivery with no stored review is
refused, and the review waiver is admitted as the waiver of that obligation.

The live pid here is this test process: the launcher records ``os.getpid()`` and
dispatch stamps the launching host on the pointer, so the run's liveness is
proven from the kernel rather than left unproven.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery

PROJECT = "proj"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

_WAIVED = (
    "the fixture promotes a delivered run whose worker is still alive to "
    "exercise the promotion plumbing; the review lifecycle has its own coverage"
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the transient crew directory at a temp tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway git repository with a docs tree and the fleet script."""
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    (root / "reckon").mkdir()
    (root / "reckon" / "target.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "docs", "reckon")
    _git(root, "commit", "-q", "-m", "chore: seed")
    return root


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _dispatch_delivered_run(repo: Path) -> dict:
    """Dispatch a run recorded against this live process, and deliver it.

    The manifest is the delivery the promotion proceeds on, so it states a
    terminal status while the recorded pid is still running as this test.
    """
    record = crew.dispatch(
        node=crew.TaskNode(
            id="node-a",
            goal="record the launch matrix for one backend",
            plan="plan-a",
            section="§3",
            done_when="uv run pytest tests/test_backends.py reports 28 passed",
            write_paths=["reckon/target.py"],
            time_budget="20m",
            spec_level="exact",
        ),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session="sess-live-worker",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
    )
    manifest = Path(record["manifest_path"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {record['node']['id']}\nstatus: complete\n", encoding="utf-8"
    )
    return record


def _commit_work(repo: Path) -> str:
    """Commit the run's declared work and return the revision it cites."""
    (repo / "reckon" / "target.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "reckon/target.py")
    _git(repo, "commit", "-q", "-m", "feat: the run's declared work")
    return _git(repo, "rev-parse", "HEAD")


def test_a_delivered_run_whose_worker_is_alive_owes_its_review(home, repo) -> None:
    record = _dispatch_delivered_run(repo)
    work = _commit_work(repo)
    run_id = record["run_id"]

    # The precondition the gate reads: the delivery is complete and the worker
    # is provably alive, so the classifier defers the run's outcome.
    assert recovery.classify_pointer(crew.read_pointer(run_id))["classification"] == (
        "running"
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[work])
    assert "no complete independent review is stored" in str(refusal.value)
    assert "--waive-unreviewed-promotion" in str(refusal.value)
    assert crew.pointer_path(run_id).exists()

    promoted = crew.complete(
        run_id, gate="passed", commits=[work], review_waiver=_WAIVED
    )

    assert promoted["record"]["review_waiver"] == {"reason": _WAIVED}
    assert promoted["pointer_removed"] is True