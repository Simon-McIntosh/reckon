"""The reflex reviews a settled head and re-reviews only what changed.

A stored review is evidence about a revision. When a reviewed run gains commits
the reflex must not buy a second full review of the whole run: it withdraws a
queued review a resume has moved past, carries the stored review forward when the
new commits change no runtime source, and dispatches only a light review of the
commits that do change runtime source — scoped to those commits alone.

Every case is driven end to end: the head move is measured from a real git
history, the carry-forward is read back through the obligation producer, and the
light review is composed through the same dispatch path the reflex uses.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.picker import client as picker_client

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "carry-forward-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

LOCAL_BACKEND = "clive"
OTHER_BACKEND = "delta"

pytestmark = pytest.mark.arms_watch_producer


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend("codex"),
        OTHER_BACKEND: _backend("claude"),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository, an isolated crew home and a reviewed implement run."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    reviewed = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                "<h2 id=\"s2\">A move re-reviews only what changed</h2>"
            ),
        },
        "feat: the reviewed change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    return {"config_home": config_home, "repo": repo, "reviewed": reviewed}


def _write_run(
    world: dict, *, run_id: str, head: str, extra: dict | None = None
) -> dict:
    """One complete, unpromoted implement run whose worktree is the repo."""
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "write_paths": ["pkg/mod.py"],
        },
    }
    pointer.update(extra or {})
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _store_review(run_id: str, *, base: str, head: str) -> None:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    stored = review_module.parse_review(emitted)
    stored.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(stored)


def _resume_stamp(minutes_ago: int) -> dict:
    return {
        "trigger": "the run was resumed",
        "at": (OBSERVED_AT - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _queued_dispatch(head: str, *, at_minutes_ago: int) -> dict:
    return {
        "status": "dispatched",
        "reason": "the review dispatched automatically",
        "run_id": "r-review-queued",
        "backend": LOCAL_BACKEND,
        "head": head,
        "at": (OBSERVED_AT - timedelta(minutes=at_minutes_ago)).isoformat(),
        "attempt": 1,
    }


def test_a_resumed_run_withdraws_a_review_queued_for_the_superseded_head(world):
    repo = world["repo"]
    reviewed = world["reviewed"]
    moved = _commit(repo, {"later.txt": "more\n"}, "feat: the run moved on")
    run_id = "r-resumed-queued-review"
    _write_run(
        world,
        run_id=run_id,
        head=moved,
        extra={
            recovery.REVIEW_DISPATCH_FIELD: _queued_dispatch(
                reviewed, at_minutes_ago=30
            ),
            "auto_resume": _resume_stamp(2),
        },
    )
    record = runs.read_pointer(run_id)

    report = recovery.withdraw_superseded_review(record)

    assert report is not None and report["withdrawn"] is True
    fresh = runs.read_pointer(run_id)
    assert fresh[recovery.REVIEW_DISPATCH_FIELD]["status"] == "withdrawn"
    assert recovery._review_in_flight(fresh) == ""


def test_a_queued_review_covering_the_current_head_is_not_withdrawn(world):
    repo = world["repo"]
    head = _git(repo, "rev-parse", "HEAD")
    run_id = "r-resumed_same"
    _write_run(
        world,
        run_id=run_id,
        head=head,
        extra={
            recovery.REVIEW_DISPATCH_FIELD: _queued_dispatch(head, at_minutes_ago=30),
            "auto_resume": _resume_stamp(2),
        },
    )
    assert recovery.withdraw_superseded_review(runs.read_pointer(run_id)) is None


def test_a_data_file_relocation_carries_the_review_forward(world, monkeypatch):
    repo = world["repo"]
    reviewed = world["reviewed"]
    run_id = "r-data-relocation"
    _write_run(world, run_id=run_id, head=reviewed)
    _store_review(run_id, base=reviewed, head=reviewed)
    new_head = _commit(
        repo, {"data/record.txt": "payload\n"}, "chore(data): land a record"
    )
    record = runs.read_pointer(run_id)

    report = recovery.carry_review_forward(record)

    assert report is not None
    assert report["carried"] is True
    assert report["scope"] == ["data/record.txt"]
    carried = review_module.read_review(PROJECT, run_id, reviewed_head_sha=new_head)
    assert carried is not None
    assert recovery.same_revision(carried["reviewed_head_sha"], new_head)

    monkeypatch.setattr(
        obligations_module,
        "_classified_rows",
        lambda _project: [recovery.classify_pointer(runs.read_pointer(run_id))],
    )
    monkeypatch.setattr(runs, "drain", lambda *_a, **_k: {"unreconciled_runs": 0})
    result = obligations_module.obligations(PROJECT, SESSION)
    kinds = [item["kind"] for item in result["obligations"]]
    assert "review-missing" not in kinds


def test_a_runtime_source_commit_earns_a_light_review_of_that_commit(world, monkeypatch):
    repo = world["repo"]
    reviewed = world["reviewed"]
    run_id = "r-light-move"
    _write_run(world, run_id=run_id, head=reviewed)
    _store_review(run_id, base=reviewed, head=reviewed)
    _commit(repo, {"pkg/mod.py": "VERSION = 2\n"}, "fix: a later source commit")

    move = recovery.review_head_move(runs.read_pointer(run_id))

    assert move["changes_runtime_source"] is True
    assert move["paths"] == ["pkg/mod.py"]
    # The decision is the shared classifier's, never a second one: forcing the
    # classifier to read the same commit as non-source flips the verdict, so the
    # move cannot be re-deriving "is this runtime source" on its own.
    monkeypatch.setattr(
        recovery.review_tiers, "changes_runtime_source", lambda _paths: False
    )
    forced = recovery.review_head_move(runs.read_pointer(run_id))
    assert forced["changes_runtime_source"] is False


def test_a_runtime_source_commit_dispatches_a_light_scoped_review(world, monkeypatch):
    repo = world["repo"]
    reviewed = world["reviewed"]
    run_id = "r-dispatch-light"
    _write_run(world, run_id=run_id, head=reviewed)
    _store_review(run_id, base=reviewed, head=reviewed)
    _commit(repo, {"pkg/mod.py": "VERSION = 3\n"}, "fix: another source commit")
    record = runs.read_pointer(run_id)

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    worktree = world["config_home"].parent / "worktrees" / run_id
    worktree.mkdir(parents=True, exist_ok=True)

    def prepare(_repo, _session, _node, base):
        return {
            "path": str(worktree),
            "base": base,
            "base_sha": _git(repo, "rev-parse", "HEAD"),
        }

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)

    from reckon import crew

    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            report = recovery.dispatch_review_for_run(
                record, config=CONFIG, launcher=lambda *a, **k: os.getpid()
            )
    finally:
        if crew.watch_state(PROJECT)["watcher_live"]:
            recovery.unwatch(PROJECT)

    assert report["dispatched"] is True, report.get("reason")
    assert report["review_tier"] == "light"
    assert report["scope"] == ["pkg/mod.py"]


# ── The review-need judge decides a move the rule would send back ───────────


class _StubJev:
    """Answer the judge's question with one probability and record each request."""

    def __init__(self, probability: float) -> None:
        self.probability = probability
        self.requests: list[tuple[dict, dict]] = []

    def __call__(self, state, questions, *, env_path):
        self.requests.append((state, questions))
        return {
            "model": "stub",
            "answers": {
                key: {"type": "noul", "noul": self.probability} for key in questions
            },
        }


RUN_GOAL = "expose the reviewed value"
RUN_DONE_WHEN = "the value reads 1 and its test passes"


def _source_move(world: dict, run_id: str, *, findings: list | None = None) -> dict:
    repo = world["repo"]
    reviewed = world["reviewed"]
    _write_run(
        world,
        run_id=run_id,
        head=reviewed,
        extra={
            "node": {
                "id": run_id,
                "plan": "fixture-plan",
                "write_paths": ["pkg/mod.py"],
                "goal": RUN_GOAL,
                "done_when": RUN_DONE_WHEN,
            }
        },
    )
    _store_review(run_id, base=reviewed, head=reviewed)
    if findings is not None:
        stored = review_module.read_review(PROJECT, run_id)
        review_module.store_review({**stored, "findings": findings})
    _commit(
        repo, {"pkg/mod.py": "VALUE = 1  # the reviewed value\n"}, "docs: comment it"
    )
    return runs.read_pointer(run_id)


def test_a_source_move_judged_immaterial_carries_the_clean_review(world, monkeypatch):
    stub = _StubJev(0.1)
    monkeypatch.setattr(picker_client, "ask", stub)
    record = _source_move(world, "r-judged-carry")

    report = recovery.carry_review_forward(record)

    assert report["carried"] is True
    assert report["judged"] == {"probability": pytest.approx(0.1), "source": "jev"}
    state, questions = stub.requests[0]
    assert len(questions) == 1
    assert "# the reviewed value" in state["changes"][0]["diff"]
    assert state["goals"] == {"goal": RUN_GOAL, "done_when": RUN_DONE_WHEN}


def test_a_source_move_judged_material_earns_the_light_review(world, monkeypatch):
    monkeypatch.setattr(picker_client, "ask", _StubJev(0.9))
    record = _source_move(world, "r-judged-review")

    report = recovery.carry_review_forward(record)

    assert report["carried"] is False
    assert report["review_tier"] == recovery.review_tiers.LIGHT
    assert "judged" not in report


def test_a_judge_that_cannot_answer_leaves_the_light_review(world):
    record = _source_move(world, "r-unjudged-review")

    report = recovery.carry_review_forward(record)

    assert report["carried"] is False
    assert report["review_tier"] == recovery.review_tiers.LIGHT


def test_a_review_that_raised_findings_is_never_judged(world, monkeypatch):
    stub = _StubJev(0.0)
    monkeypatch.setattr(picker_client, "ask", stub)
    record = _source_move(
        world,
        "r-repaired-review",
        findings=[{"id": "f1", "severity": "major", "text": "the value is unread"}],
    )

    report = recovery.carry_review_forward(record)

    assert stub.requests == []
    assert report["carried"] is False
