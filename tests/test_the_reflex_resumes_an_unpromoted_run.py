"""The reflex repairs an unpromoted reviewed run by resuming it, not dispatching.

An unpromoted reviewed run still holds its own worktree and its commit claim, so
a repair node over the reviewed run's own paths is refused at dispatch: the run
that owns the worktree is the only worker that can answer the finding. This test
drives a finding-bearing review of an exited, unpromoted run through the reflex
and asserts the composed repair rides a resume of the reviewed run itself, with
the finding named in the advice, while no new node is dispatched. It also fixes
the two negative halves that make the change safe: a second sweep for the same
round resumes nothing, and a promoted reviewed run is not resumed at all.

The participant ports — the resume entry point and the dispatch — are both
stubbed, so no worker is launched and no call reaches a scheduler.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery, repair, resumption, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-unpromoted"
NODE_ID = "a-reviewed-node"

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

# One blocking finding naming a repository source path, so the round composes a
# repair and the only thing that can decide resume-versus-dispatch is the
# reflex's own logic rather than a record-only decline.
FINDING = {
    "file": "reckon/crew/thing.py",
    "line": "10",
    "text": "off-by-one in the loop",
}


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
    """A project whose review store, ledger and repo all live under a temp root."""
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
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s2">An unpromoted reviewed run is resumed</h2>',
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


def _config_home() -> Path:
    return Path(os.environ["RECKON_HOME"])


def _completed_pointer(
    config_home: Path, repo: Path, *, role: str = "implement"
) -> dict:
    """The reviewed run: an exited implement run whose live pointer still stands.

    ``process_alive`` is False and the run records no pid, so the reflex reads
    its worker as exited. The node declares no write path, so the reviewed run
    holds no claim that could itself decide the reflex's path.
    """
    manifest = config_home / "manifests" / (RUN_ID + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: " + RUN_ID + "\nstatus: complete\ncommits: " + RUN_ID + "\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "role": role,
        "node": {"id": NODE_ID, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _store_review(head_sha: str, findings: list[dict[str, str]]) -> None:
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "total": 75,
        "findings": findings,
    }
    review_module.store_review(record)


def _wait_for_stopped_producer() -> None:
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if not crew.watch_state(PROJECT)["watcher_live"]:
            return
        time.sleep(0.05)
    pytest.fail("watch producer did not release its seat")


@contextmanager
def _armed_fleet():
    """Hold the follower claim the sweep is gated on, then release the watch."""
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            if crew.watch_state(PROJECT)["watcher_live"]:
                recovery.unwatch(PROJECT)
                _wait_for_stopped_producer()


def _stub_resume(monkeypatch, *, turn: int = 1) -> list[dict]:
    """Replace the resume entry point so the call and its advice can be read."""
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice, "launcher": launcher})
        return {"pid": os.getpid(), "turn": turn, "log_path": f"resume-{turn}.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    return calls


def _stub_dispatch(monkeypatch) -> list[dict]:
    """Replace the dispatch so any node launch is recorded rather than run."""
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-whatever"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    return calls


def _sweep() -> dict:
    with _armed_fleet():
        return resumption.sweep(PROJECT, config=CONFIG)


def test_an_unpromoted_exited_run_is_resumed_not_dispatched(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The positive half: the finding rides a resume of the reviewed run."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    _sweep()

    # Exactly one resume of the reviewed run, carrying advice that names the one
    # finding by its content-derived id, and no node dispatched for the round.
    assert len(resumed) == 1
    assert resumed[0]["run_id"] == RUN_ID
    expected_id = repair.finding_id(FINDING)
    assert expected_id in resumed[0]["advice"]
    assert FINDING["file"] in resumed[0]["advice"]
    assert dispatched == []

    # The round is recorded as resumed, with the id the composer minted for it.
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"
    assert recorded["round_id"]
    assert recorded["node_id"]


def _write_live_worker_record() -> None:
    """Record a live worker pid on the run, as the resumed supervisor would."""
    directory = Path(runs.run_dir(RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )


def test_a_second_sweep_while_the_resumed_worker_is_live_resumes_nothing(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """In flight only while the worker lives: a live resumed turn is left alone."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    _sweep()
    # The resumed turn is now running; its own worker record makes it live.
    _write_live_worker_record()
    _sweep()

    assert len(resumed) == 1
    assert dispatched == []


def test_an_exited_resumed_worker_gets_one_more_resume(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A resumed turn that ends without answering does not strand the round.

    The round is in flight only while the worker is live, so once that worker
    has exited and the run's head has not moved past the reviewed head, the
    reflex resumes the round once more rather than leaving it recorded as
    resumed forever; the attempt count records the retry.
    """
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    first = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert first["status"] == "resumed"
    assert first["attempt"] == 1

    # The resumed worker ended without writing a live record: the round is free
    # again, so the next sweep resumes once more.
    _sweep()

    assert len(resumed) == 2
    second = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert second["status"] == "resumed"
    assert second["attempt"] == 2


def test_a_promoted_reviewed_run_is_not_resumed(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A promoted run is settled: no resume, and its dispatch still refuses."""
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(head_sha, [FINDING])
    run_file = ledger.run_path(PROJECT, RUN_ID)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    crew._write_json(run_file, {"run_id": RUN_ID, "status": "promoted"})
    resumed = _stub_resume(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    _sweep()

    assert resumed == []
    assert dispatched == []
    recorded = runs.read_pointer(RUN_ID).get("repair_dispatch") or {}
    assert recorded.get("status") != "resumed"
