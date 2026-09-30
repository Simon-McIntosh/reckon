"""A resumed run's stale terminal manifest does not license promotion.

Recovery reads a run whose worker was launched after its manifest's last write
as working: the file beside the pointer carries the verdict a superseded
attempt left, so it states nothing about the work running now. Promotion read
the same file as delivery, so ``crew complete`` on such a run recorded the
previous attempt's outcome and deleted the live pointer under a worker still
mid-turn.

The fixture is the pair recovery already defers. A run is dispatched against
this live test process, so the pointer names a running pid; a complete manifest
is written and then ordered against a worker record naming the same process.
The ordering is set with ``os.utime`` rather than waited for, so the two cases
below differ only in which of the manifest and the launch is the later one.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs

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


def _dispatch_live_run(repo: Path) -> dict:
    """Dispatch a run whose pointer names this live process."""
    return crew.dispatch(
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
        session="sess-stale-manifest",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
    )


def _stamp(seconds_before_now: float) -> str:
    """A UTC stamp in the form a worker record carries, offset from now."""
    moment = datetime.now(UTC) - timedelta(seconds=seconds_before_now)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_manifest(record: dict, seconds_before_now: float) -> Path:
    """Write the run's delivered manifest, dated by the caller."""
    manifest = Path(record["manifest_path"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {record['node']['id']}\nstatus: complete\n", encoding="utf-8"
    )
    written = time.time() - seconds_before_now
    os.utime(manifest, (written, written))
    return manifest


def _write_worker_record(record: dict, *, seconds_before_now: float) -> Path:
    """Write the launch record a supervisor leaves for the attempt it started."""
    worker = runs.run_dir(record["run_id"]) / recovery.WORKER_RECORD_NAME
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_text(
        json.dumps(
            {
                "run_id": record["run_id"],
                "attempt": int(record.get("attempt") or 1),
                "pid": os.getpid(),
                "pid_start_time": runs._process_start_time(os.getpid()),
                "launched_at": _stamp(seconds_before_now),
                "backend": "alpha",
                "argv": [],
            }
        ),
        encoding="utf-8",
    )
    return worker


def _commit_work(repo: Path) -> str:
    """Commit the run's declared work and return the revision it cites."""
    (repo / "reckon" / "target.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "reckon/target.py")
    _git(repo, "commit", "-q", "-m", "feat: the run's declared work")
    return _git(repo, "rev-parse", "HEAD")


def test_a_manifest_older_than_the_attempt_does_not_license_promotion(
    home, repo
) -> None:
    record = _dispatch_live_run(repo)
    work = _commit_work(repo)
    run_id = record["run_id"]

    # The previous attempt's verdict, written before the attempt now running.
    _write_manifest(record, seconds_before_now=120)
    _write_worker_record(record, seconds_before_now=60)

    # The precondition recovery already reads: a live worker launched after its
    # manifest defers the run's outcome rather than calling it delivered.
    assert recovery.classify_pointer(crew.read_pointer(run_id))["classification"] == (
        "running"
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[work])
    refusal_text = str(refusal.value)
    assert "cannot be promoted" in refusal_text
    assert "was written before this attempt was launched" in refusal_text
    assert "--waive-live-run" in refusal_text
    # Nothing landed: the refusal precedes every store write.
    assert crew.pointer_path(run_id).exists()


def test_a_manifest_newer_than_the_attempt_licenses_promotion(home, repo) -> None:
    record = _dispatch_live_run(repo)
    work = _commit_work(repo)
    run_id = record["run_id"]

    # The same run, with the verdict written by the attempt now running.
    _write_worker_record(record, seconds_before_now=120)
    _write_manifest(record, seconds_before_now=60)

    promoted = crew.complete(
        run_id, gate="passed", commits=[work], review_waiver=_WAIVED
    )

    assert promoted["record"]["review_waiver"] == {"reason": _WAIVED}
    assert promoted["pointer_removed"] is True