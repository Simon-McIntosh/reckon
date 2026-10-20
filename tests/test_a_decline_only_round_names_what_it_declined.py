"""A decline-only repair round states its decision and names what it left.

The reflex declines a round when no *blocking* finding cites a path a repair
could be granted: a follow-on citing source cannot carry a round whose blocking
findings cite only the fleet's own record. That decision is deliberate, but its
old recorded reason said ``no finding cites a repository path`` — a wider claim
than the decision, and one that reads as false whenever a follow-on did cite a
repository path. A coordinator reading it concludes the review raised nothing
repairable, and the sub-floor dimension the review also carried has no stated
next step in the round record.

These cases drive the decline-only branch through the reflex with the dispatch
and the resume stubbed. The first asserts the reason states the blocking-only
decision and names both the sub-floor dimension and the repository paths the
follow-on findings cite. The second asserts a round whose only findings cite the
fleet's own record names neither. The third asserts a blocking finding citing a
source path still composes a repair, so the naming change alters no decision.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, resumption, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch import WATCHER_LOAD_BOUND_SECONDS

# The reflex is gated by the watch admission, so these tests arm the producer
# the gate reads rather than accepting the suite-wide waiver.
pytestmark = pytest.mark.arms_watch_producer

PROJECT = "sample"
RUN_ID = "r-work"
NODE_ID = "a-reviewed-node"

# The project declares a floor under which a dimension's low score is named
# rather than averaged away in the review's total.
FLOOR_DIMENSION = "reuse"
FLOOR_SCORE = 10

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
    "gates": {"dimension_floors": {FLOOR_DIMENSION: FLOOR_SCORE}},
}


def _finding(
    path: str, line: str, text: str, severity: str | None = None
) -> dict[str, str]:
    finding = {"file": path, "line": line, "text": text}
    if severity is not None:
        finding["severity"] = severity
    return finding


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
        '<h2 id="s2">A finding-bearing review dispatches its repair</h2>',
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


def _completed_pointer(
    config_home: Path,
    repo: Path,
    *,
    role: str = "implement",
    node_id: str = NODE_ID,
    **extra: object,
) -> dict:
    """The reviewed run: a completed implement run whose manifest reports completion."""
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
        "node": {"id": node_id, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    record.update(extra)
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return record


def _store_review(
    head_sha: str,
    findings: list[dict[str, str]],
    *,
    scores: dict[str, int] | None = None,
) -> None:
    """Write the reviewed run's review record into the isolated store root."""
    scored = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15)
    scored.update(scores or {})
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "status": "parsed",
        "scores": scored,
        "total": sum(scored.values()),
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
    """Hold the follower claim the dispatch is gated on, then release the watch."""
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        try:
            yield
        finally:
            if crew.watch_state(PROJECT)["watcher_live"]:
                recovery.unwatch(PROJECT)
                _wait_for_stopped_producer()


@contextmanager
def _stubbed_dispatch(monkeypatch):
    """Replace the dispatch with a recorder so a launch call can be read."""
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    yield calls


@contextmanager
def _stubbed_resume(monkeypatch):
    """Replace the resume entry point the unpromoted path uses."""
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice})
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    yield calls


def test_a_decline_only_round_names_its_decision_and_what_it_left(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """The reason states the blocking-only decision and names the dimension and files.

    The blocking finding cites the run's own gate log under the crew home, so no
    blocking finding cites a repairable path and the round declines. The review
    also carries a sub-floor reuse dimension and two follow-on findings citing a
    source file and a test file, so a coordinator reading the round record must
    see both named rather than conclude the review raised nothing repairable.
    """
    config_home, repo, head_sha = isolated_project
    run_dir = Path(runs.run_dir(RUN_ID))
    _completed_pointer(config_home, repo)
    _store_review(
        head_sha,
        [
            _finding(
                str(run_dir / "gate.log"),
                "6",
                "the gate log ends without a verdict",
                "blocking",
            ),
            _finding(
                "reckon/crew/queue_admission.py",
                "412",
                "the admission pass records no verdict",
                "follow-on",
            ),
            _finding(
                "tests/test_each_admission_pass_records_its_verdict.py",
                "88",
                "the new test does not exercise the refusal",
                "follow-on",
            ),
        ],
        scores={FLOOR_DIMENSION: 4},
    )
    with (
        _stubbed_dispatch(monkeypatch) as calls,
        _stubbed_resume(monkeypatch),
        _armed_fleet(),
    ):
        resumption.sweep(PROJECT, config=CONFIG)

    assert calls == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "decline-only"
    reason = recorded["reason"]
    assert "no blocking finding cites a repairable path" in reason
    assert FLOOR_DIMENSION in reason
    assert f"4/{FLOOR_SCORE}" in reason
    assert "reckon/crew/queue_admission.py" in reason
    assert "tests/test_each_admission_pass_records_its_verdict.py" in reason
    assert str(run_dir / "gate.log") not in reason


def test_a_round_of_record_paths_names_neither(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A round whose only findings cite crew-home paths names neither dimension nor file.

    With no dimension below its floor and no follow-on citing a repository path,
    the reason carries the decision alone: the wider claim that a finding cited a
    repository path is not restated when none did.
    """
    config_home, repo, head_sha = isolated_project
    run_dir = Path(runs.run_dir(RUN_ID))
    _completed_pointer(config_home, repo)
    _store_review(
        head_sha,
        [
            _finding(
                str(run_dir / "manifest.md"),
                "1",
                "the manifest reports the wrong count",
                "blocking",
            ),
            _finding(
                str(run_dir / "gate.log"),
                "6",
                "the gate log ends without a verdict",
                "follow-on",
            ),
        ],
    )
    with (
        _stubbed_dispatch(monkeypatch) as calls,
        _stubbed_resume(monkeypatch),
        _armed_fleet(),
    ):
        resumption.sweep(PROJECT, config=CONFIG)

    assert calls == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "decline-only"
    reason = recorded["reason"]
    assert "no blocking finding cites a repairable path" in reason
    assert "sub-floor" not in reason
    assert FLOOR_DIMENSION not in reason
    assert "follow-on finding" not in reason
    assert str(run_dir / "gate.log") not in reason


def test_a_blocking_source_finding_still_composes_a_repair(
    isolated_project: tuple[Path, Path, str], monkeypatch
) -> None:
    """A blocking finding citing a repository path still composes, so the decision is unchanged.

    The naming change touches only the decline-only reason text; a round whose
    blocking finding cites a repository source path resumes with the composed
    advice, exactly as before.
    """
    config_home, repo, head_sha = isolated_project
    _completed_pointer(config_home, repo)
    _store_review(
        head_sha,
        [
            _finding(
                "reckon/crew/queue_admission.py",
                "412",
                "the admission pass records no verdict",
                "blocking",
            )
        ],
        scores={FLOOR_DIMENSION: 4},
    )
    with _stubbed_resume(monkeypatch) as calls, _armed_fleet():
        resumption.sweep(PROJECT, config=CONFIG)

    assert len(calls) == 1
    assert "reckon/crew/queue_admission.py" in calls[0]["advice"]
