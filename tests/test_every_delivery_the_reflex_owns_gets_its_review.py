"""Every delivery the reflex owns gets its review.

A delivery whose review the session's reflex owns must be reviewed by that
reflex, or the review-missing duty must name why it was not and what to run by
hand. Three cases: a first delivery, a repair delivery after a resume, and a
delivery whose reflex dispatch was refused.

The reflex is the sweep a follower runs, so each case drives a real sweep over
a live pointer with an armed follower; the dispatch is asserted against a stub
launcher that records its calls, because the claim is that the sweep issued the
dispatch, and an issued command is visible in the recorded call while a review
that ran and wrote nothing is not.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import obligations, recovery, resumption, runs
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
SESSION = "session-orchestrating"

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s14">Every delivery the reflex owns gets its review</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    return config_home, repo, base_sha


def _manifest(config_home: Path, run_id: str, *, status: str = "complete") -> Path:
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: {run_id}\n", encoding="utf-8"
    )
    return manifest


def _pointer(
    config_home: Path,
    repo: Path,
    run_id: str,
    *,
    head: str,
    session: str = SESSION,
    extra: dict | None = None,
) -> dict:
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "created_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "s14"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "working",
        "process_alive": False,
        "session": session,
        "manifest_path": str(_manifest(config_home, run_id)),
        "base_sha": head,
    }
    if extra:
        record.update(extra)
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


def _release_watch() -> None:
    if crew.watch_state(PROJECT)["watcher_live"]:
        recovery.unwatch(PROJECT)
        _wait_for_stopped_producer()


@contextmanager
def _armed_fleet() -> None:
    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        try:
            yield
        finally:
            _release_watch()


def _launcher_recording(calls: list[dict]):
    def launcher(plan, *, log_path, stderr_path, prompt_path):
        calls.append(
            {
                "argv": list(getattr(plan, "argv", ())),
                "log_path": str(log_path),
                "prompt_path": str(prompt_path),
            }
        )
        return os.getpid()

    return launcher


def test_a_first_delivery_is_reviewed_by_the_reflex(
    isolated_project: tuple[Path, Path, str],
) -> None:
    config_home, repo, head = isolated_project
    _pointer(config_home, repo, "r-first", head=head)
    calls: list[dict] = []
    with _armed_fleet():
        report = resumption.sweep(
            PROJECT, config=CONFIG, launcher=_launcher_recording(calls)
        )
    assert report["reviews"]["dispatched"], report["reviews"]
    assert len(calls) == 1
    assert runs.read_pointer("r-first")["review_dispatch"]["status"] == "dispatched"


def test_a_repair_delivery_is_reviewed_by_the_reflex(
    isolated_project: tuple[Path, Path, str],
) -> None:
    """A repair round's delivery at the head its resume carried.

    The reflex resumed this run to answer a blocking finding and the round has
    now delivered at the same revision. The delivery is settled, so it owes a
    review, and the reflex is the actor that owns it.
    """
    config_home, repo, head = isolated_project
    resumed_at = datetime.now(UTC) - timedelta(minutes=40)
    delivered_at = time.time() - 60
    _pointer(
        config_home,
        repo,
        "r-repaired",
        head=head,
        extra={
            "attempt": 3,
            "attempt_kind": "resume",
            # Repair tooling rewrote the manifest after this resume began, so
            # its mtime does not clear the baseline the resume recorded.
            "manifest_baseline_mtime_ns": int((delivered_at + 30) * 1_000_000_000),
            "resume_superseded_head": {
                "head": head,
                "at": resumed_at.isoformat(),
            },
            "repair_dispatch": {
                "round_id": "r-repaired@1",
                "status": "resumed",
                "run_id": "r-repaired",
                "at": resumed_at.isoformat(),
            },
        },
    )
    manifest = Path(str(runs.read_pointer("r-repaired")["manifest_path"]))
    stamp = delivered_at
    os.utime(manifest, (stamp, stamp))
    # The supervisor's exit record is the attempt's own proof that it has
    # ended; its launch precedes the manifest write.
    exit_record = runs.run_dir("r-repaired") / "attempt-3-exit.json"
    exit_record.parent.mkdir(parents=True, exist_ok=True)
    exit_record.write_text(
        json.dumps(
            {
                "run_id": "r-repaired",
                "attempt": 3,
                "exit_code": 0,
                "launched_at": (datetime.now(UTC) - timedelta(minutes=35)).isoformat(),
                "exited_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict] = []
    with _armed_fleet():
        report = resumption.sweep(
            PROJECT, config=CONFIG, launcher=_launcher_recording(calls)
        )
    assert report["reviews"]["dispatched"], report["reviews"]
    assert len(calls) == 1


def test_an_overdue_duty_names_the_reflex_refusal(
    isolated_project: tuple[Path, Path, str],
    monkeypatch,
) -> None:
    """A delivery whose reflex dispatch was refused names that refusal.

    The reflex records the refusal it reached on the run it acted for. While the
    duty is inside the reflex's window it waits; past the window it must name
    the refusal and the command that dispatches the review by hand instead of
    sending its reader to inspect a follower with nothing wrong with it.
    """
    config_home, repo, head = isolated_project
    record = _pointer(
        config_home,
        repo,
        "r-refused",
        head=head,
        extra={
            "resume_launch_in_flight": {
                "at": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
                "attempt": 4,
            }
        },
    )
    command = f"reckon crew dispatch --project {PROJECT} --role review --run r-refused"
    window = obligations._reflex_review_window_seconds()
    row = {
        "run_id": "r-refused",
        "node": "r-refused",
        "plan": "fixture",
        "classification": "scoring",
        "age_seconds": window + 600,
        "head": head,
        "next_action": command,
    }
    with _armed_fleet():
        duty = obligations._review_duty_item(
            PROJECT,
            row,
            record,
            now=datetime.now(UTC),
            grace=60,
            live_pointers=[record],
            placements={},
        )
    assert duty is not None, row
    refusal = "the run has a resume launching; its delivery is not settled"
    assert duty["kind"] == "review-missing", duty
    assert refusal in duty["reason"], duty
    assert command in duty["next_command"], duty
    assert "inspect the follower" not in duty["next_command"], duty
