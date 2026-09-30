"""A resumed run's stale terminal manifest does not license promotion.

Recovery reads a run whose worker was launched after its manifest's last write
as working: the file beside the pointer carries the verdict a superseded
attempt left, so it states nothing about the work running now. Promotion read
the same file as delivery, so ``crew complete`` on such a run recorded the
previous attempt's outcome and deleted the live pointer under a worker still
mid-turn.

Two shapes reach the guard here and they are not the same defect. A first
dispatch leaves its manifest fresh by the run's own baseline, so the attempt's
own terminal status has to be judged against its launch time. A real resume
sets its baseline to the inherited manifest's mtime, so the inherited status is
never fresh and the attempt has written no verdict at all -- refusing that is
the rescission the resume forces, because the worker it just started is alive
and would be orphaned by a promotion.

The fixture dispatches a run against this live test process, so the pointer
names a running pid, and it orders the manifest against the attempt with
``os.utime`` or the real resumption record rather than waiting.
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
from reckon.crew.dispatch import record_resumption

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

_NO_MANIFEST_YET = "no manifest written by this attempt is on file"


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


def _resume_live_run(record: dict) -> dict:
    """Resume the run the way the resume command does.

    ``record_resumption`` is the writer the command itself calls: it records the
    new attempt's pid and takes its manifest baseline from the manifest already
    on disk, so the inherited manifest is not fresh for the attempt it starts.
    """
    directory = runs.run_dir(record["run_id"])
    return record_resumption(
        record["run_id"],
        pid=os.getpid(),
        turn=1,
        log_path=directory / "resume-1.jsonl",
        stderr_path=directory / "resume-1.stderr.log",
        attempt_started_at=runs._utc_now(),
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


def test_a_resumed_run_with_no_manifest_of_its_own_is_refused(home, repo) -> None:
    record = _dispatch_live_run(repo)
    work = _commit_work(repo)
    run_id = record["run_id"]

    # The attempt before this one delivered, and this one has not: the resume
    # command takes the inherited manifest's mtime as the new baseline, and the
    # supervisor records the launch of the worker it started.
    _write_manifest(record, seconds_before_now=120)
    _write_worker_record(record, seconds_before_now=0)
    _resume_live_run(record)

    fresh = crew.read_pointer(run_id)
    assert fresh["manifest_baseline_mtime_ns"] == runs._manifest_mtime_ns(
        record["manifest_path"]
    )
    assert runs._manifest_freshness(fresh) == (True, False)
    assert recovery._worker_launched_after_manifest(
        fresh, Path(record["manifest_path"])
    )
    # The precondition: recovery reads the resumed run as working.
    assert recovery.classify_pointer(fresh)["classification"] == "running"

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[work])
    refusal_text = str(refusal.value)
    assert "cannot be promoted" in refusal_text
    assert _NO_MANIFEST_YET in refusal_text
    assert "--waive-live-run" in refusal_text
    assert crew.pointer_path(run_id).exists()


def test_a_live_attempt_that_has_written_no_manifest_is_refused(home, repo) -> None:
    record = _dispatch_live_run(repo)
    work = _commit_work(repo)
    run_id = record["run_id"]

    # A run in progress: the worker is alive and has delivered nothing yet.
    assert not Path(record["manifest_path"]).exists()

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[work])
    assert _NO_MANIFEST_YET in str(refusal.value)
    assert crew.pointer_path(run_id).exists()


def test_a_manifest_older_than_the_attempt_does_not_license_promotion(
    home, repo
) -> None:
    record = _dispatch_live_run(repo)
    work = _commit_work(repo)
    run_id = record["run_id"]

    # A first dispatch: the manifest is fresh for this attempt by its baseline,
    # so its terminal status has to be judged against the attempt's launch.
    _write_manifest(record, seconds_before_now=120)
    _write_worker_record(record, seconds_before_now=60)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[work])
    refusal_text = str(refusal.value)
    assert "cannot be promoted" in refusal_text
    assert "was written before this attempt was launched" in refusal_text
    assert "--waive-live-run" in refusal_text
    assert crew.pointer_path(run_id).exists()


def test_a_manifest_newer_than_the_attempt_licenses_promotion(home, repo) -> None:
    record = _dispatch_live_run(repo)
    work = _commit_work(repo)
    run_id = record["run_id"]

    # The same run, with the verdict written by the attempt now running.
    _write_worker_record(record, seconds_before_now=120)
    _write_manifest(record, seconds_before_now=60)

    # The verdict is the attempt's own, so the live-run guard has nothing to
    # say and the next gate refuses: the classification is stated as its own
    # fact, never as the consequence of the review the run does not have.
    with pytest.raises(crew.CrewError) as unreviewed:
        crew.complete(run_id, gate="passed", commits=[work])
    review_refusal = str(unreviewed.value)
    assert "is classified running" in review_refusal
    assert "no complete independent review is stored" in review_refusal
    assert "because no complete independent review" not in review_refusal

    promoted = crew.complete(
        run_id, gate="passed", commits=[work], review_waiver=_WAIVED
    )

    assert promoted["record"]["review_waiver"] == {"reason": _WAIVED}
    assert promoted["pointer_removed"] is True
