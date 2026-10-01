"""The reflex opens at most one automatic repair round for a reviewed run.

A review round is the pair a stored record stands for — the run it read and the
head it read — and one round is one repair. The identity keyed on the head is
what the composer already uses, and it is exactly what lets a *later* head open
a second round: a field run was resumed on three successive reviews scoring 82,
83 and 83 because each new head drew a new review, each new review composed a new
round, and each new round fired its own repair. The cap this file drives is
run-wide rather than round-local: once one round has opened, a later round hands
the run back to its coordinator instead of opening another.

The participant ports — the resume entry point and the dispatch — are both
stubbed, so no worker is launched and no call reaches a scheduler.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

from reckon import crew
from reckon.crew import recovery, resumption, runs
from reckon.crew import review as review_module

PROJECT = "sample"
RUN_ID = "r-reviewed"
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

# One blocking finding naming a repository source path, so each round composes a
# repair and the only thing that can decide whether it fires is the round cap
# rather than a record-only decline.
FINDING = {
    "file": "reckon/crew/thing.py",
    "line": "10",
    "text": "off-by-one in the loop",
}


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path, str]:
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
        '<h2 id="s2">A run opens at most one repair round</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _git(repo, "add", "seed.txt", "skills", "docs/plans/fixture.html")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        '{"sample": "' + str(repo / "docs") + '"}', encoding="utf-8"
    )
    return config_home, repo, _git(repo, "rev-parse", "HEAD")


def _advance_head(repo: Path) -> str:
    """Commit a new revision and return it, so the run is reviewed at a new head."""
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "add", "second.txt")
    _git(repo, "commit", "-q", "-m", "chore: advance")
    return _git(repo, "rev-parse", "HEAD")


def _completed_pointer(config_home: Path, repo: Path) -> dict:
    """An exited, unpromoted implement run whose live pointer still stands."""
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
        "role": "implement",
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


def _write_finished_resume_stream() -> None:
    """Write the finished shape a resume that ran leaves: assistant then result."""
    directory = Path(runs.run_dir(RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "resume-1.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": "working"}})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success"})
        + "\n",
        encoding="utf-8",
    )


def _stub_resume(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        calls.append({"run_id": run_id, "advice": advice})
        _write_finished_resume_stream()
        return {"pid": os.getpid(), "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    return calls


def _stub_dispatch(monkeypatch) -> list[dict]:
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)
    return calls


def _dispatch(record: dict, config: dict | None = None) -> dict:
    """Drive the reflex's repair dispatch with the pointers as they stand."""
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        return recovery.dispatch_repair_for_run(
            record, config=config or CONFIG, launcher=lambda *a, **k: os.getpid()
        )


def _durable_pointer() -> dict:
    return runs.read_pointer(RUN_ID)


def test_the_first_round_resumes(tmp_path: Path, monkeypatch) -> None:
    """The positive half: with no round opened, the reflex resumes the repair."""
    config_home, repo, head_sha = _fixture(tmp_path, monkeypatch)
    record = _completed_pointer(config_home, repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    report = _dispatch(record)

    assert report["resumed"] is True
    assert len(resumed) == 1 and resumed[0]["run_id"] == RUN_ID
    opened = _durable_pointer()["repair_rounds"]
    assert opened["count"] == 1
    assert opened["round_id"].endswith(head_sha)


def test_a_second_round_on_a_new_head_hands_back(tmp_path: Path, monkeypatch) -> None:
    """The cap: a later head reviewed after the repair opens no second round.

    Round one opens and resumes at the first head. The run then moves to a new
    head, which draws a new review and composes a *different* round. That round
    must open nothing: the reflex records the hand-back, names the earlier round
    and its attempts beside the new review's score, and resumes nothing.
    """
    config_home, repo, first_head = _fixture(tmp_path, monkeypatch)
    record = _completed_pointer(config_home, repo)
    _store_review(first_head, [FINDING])
    resumed = _stub_resume(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch)

    first = _dispatch(record)
    assert first["resumed"] is True and len(resumed) == 1

    second_head = _advance_head(repo)
    assert second_head != first_head
    _store_review(second_head, [FINDING])

    second = _dispatch(record)

    assert second["dispatched"] is False
    assert second.get("handed_to_coordinator") is True
    assert len(resumed) == 1  # the second round resumed nothing
    assert dispatched == []  # and dispatched nothing

    recorded = _durable_pointer()["repair_dispatch"]
    assert recorded["status"] == "handed-to-coordinator"
    reason = recorded["reason"]
    assert first_head in reason  # names the earlier round
    assert "1 attempt" in reason  # names the earlier round's attempts
    assert "75" in reason  # names the new review's score

    # The opened-round record is untouched by the hand-back: the run still shows
    # exactly one round opened, at the first head.
    opened = _durable_pointer()["repair_rounds"]
    assert opened["count"] == 1
    assert opened["round_id"].endswith(first_head)


def test_the_in_round_retry_still_works_in_round_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The retry is unchanged: a finished resumed turn in round one resumes again."""
    config_home, repo, head_sha = _fixture(tmp_path, monkeypatch)
    record = _completed_pointer(config_home, repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)  # a finished stream on every turn
    _stub_dispatch(monkeypatch)

    _dispatch(record)
    _dispatch(record)

    assert len(resumed) == 2
    opened = _durable_pointer()["repair_rounds"]
    assert opened["count"] == 1  # a retry is not a second round
    assert opened["attempts"] == 2
