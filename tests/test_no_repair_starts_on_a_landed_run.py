"""No repair resume starts for a run whose reviewed head has landed.

A review-driven repair answers findings raised against a delivered run by
resuming that run's worker. When the run has already landed, the same resume
commits on top of a landed delivery: the run's record then names a commit the
primary branch does not carry, its manifest reads unreadable, and no review of
the new commit can be composed. So the reflex refuses every kind of resume for
a landed head and records the round's work against its section instead — the
findings deferred on the stored review, where a later node finds them as open
work, and the round recorded on the run's own repair record.

Three cases pin the rule. A landed run whose review scores a dimension below
its declared floor gets no resume and the dimension is carried. A landed run
with a blocking finding gets none either, and the finding is deferred rather
than exempted, because the reflex judges nothing. An unlanded run with the same
review is resumed as before, so the guard cannot pass by refusing every run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption, runs
from reckon.crew import review as review_module
from tests.helpers.git import git

PROJECT = "sample"
RUN_ID = "r-20260101T000000000000-landed-delivery"
NODE_ID = "a-landed-node"
SECTION = "s2"
PLAN_SLUG = "fixture"
SOURCE = "reckon/crew/guard.py"
ORIGINAL = "def guard():\n    return True\n"

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
    "gates": {"dimension_floors": {"durability": 10}},
}

BLOCKING = {
    "file": SOURCE,
    "line": "2",
    "text": "the guard returns true without reading its argument",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A project whose review store, live pointer and repository are disposable.

    The repository is real: the landing case is decided by reading the shared
    checkout's branch, so a stubbed git would prove nothing. The resume and
    dispatch entry points are replaced with recorders, because the claim under
    test is that neither is called.
    """
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN_SLUG}.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        f'<h2 id="{SECTION}">A landed run carries its findings to its section</h2>',
        encoding="utf-8",
    )
    (repo / SOURCE).parent.mkdir(parents=True)
    (repo / SOURCE).write_text(ORIGINAL, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "worker@example.invalid")
    git(repo, "config", "user.name", "Worker")
    git(repo, "add", SOURCE, f"docs/plans/{PLAN_SLUG}.html")
    git(repo, "commit", "-q", "-m", "chore: seed the delivered path")
    base = git(repo, "rev-parse", "HEAD")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    resumed: list[dict[str, object]] = []
    dispatched: list[dict[str, object]] = []

    def fake_resume(run_id, record, *, config=None, launcher=None, advice=""):
        resumed.append({"run_id": run_id, "advice": advice})
        return {"pid": 1, "turn": 1, "log_path": "resume-1.jsonl"}

    monkeypatch.setattr(resumption, "_resume", fake_resume)
    import importlib

    dispatch_module = importlib.import_module("reckon.crew.dispatch")

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return {"run_id": "r-repair-stub"}

    monkeypatch.setattr(dispatch_module, "dispatch", fake_dispatch)

    def deliver(name: str) -> dict[str, str]:
        """A delivered run on its own worktree, one commit past the base."""
        worktree = tmp_path / name
        git(repo, "worktree", "add", "--detach", str(worktree), base)
        (worktree / SOURCE).write_text(
            ORIGINAL.replace("return True", "return bool(1)"), encoding="utf-8"
        )
        git(worktree, "add", SOURCE)
        git(worktree, "commit", "-q", "-m", "feat: deliver the guarded predicate")
        head = git(worktree, "rev-parse", "HEAD")
        manifest = tmp_path / f"{name}-manifest.md"
        manifest.write_text(
            "node: " + NODE_ID + "\nstatus: complete\ncommits: " + head + "\n",
            encoding="utf-8",
        )
        pointer = {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(worktree),
            "manifest_path": str(manifest),
            "base_sha": base,
            "launch": "cli",
            "role": "implement",
            "backend": "alpha",
            "argv": ["codex"],
            "process_alive": False,
            "session": "session-orchestrating",
            "created_at": "2026-01-01T00:00:00Z",
            "node": {
                "id": NODE_ID,
                "plan": PLAN_SLUG,
                "section": SECTION,
                "role": "implement",
                "time_budget": "25m",
                "write_paths": [SOURCE],
            },
        }
        runs._write_json(runs.pointer_path(RUN_ID), pointer)
        return {
            "head": head,
            "base": base,
            "worktree": str(worktree),
            "manifest": str(manifest),
            "pointer": pointer,
        }

    def store_review(
        *,
        head: str,
        base: str,
        findings: list[dict[str, str]] | None = None,
        scores: dict[str, int] | None = None,
    ) -> None:
        stored_scores = scores or dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18)
        review_module.store_review(
            {
                "project": PROJECT,
                "reviewed_run_id": RUN_ID,
                "review_run_id": "review-of-the-delivery",
                "reviewed_base_sha": base,
                "reviewed_head_sha": head,
                "status": "parsed",
                "scores": stored_scores,
                "total": sum(stored_scores.values()),
                "findings": list(findings or []),
            }
        )

    def land(head: str) -> str:
        """Land the delivery the way the lander does: fast-forward primary to it."""
        git(repo, "merge", "--ff-only", "-m", "merge: land the delivery", head)
        return git(repo, "rev-parse", "HEAD")

    return {
        "home": home,
        "repo": repo,
        "base": base,
        "resumed": resumed,
        "dispatched": dispatched,
        "deliver": deliver,
        "store_review": store_review,
        "land": land,
    }


def _dispatch(world: dict[str, object]) -> dict[str, object]:
    pointer = runs.read_pointer(RUN_ID) or {}
    with runs.follower_claim(PROJECT, "session-orchestrating", delivery="stream"):
        return recovery.dispatch_repair_for_run(
            pointer, config=CONFIG, launcher=lambda *a, **k: 1
        )


def _stored_record(head: str):
    return review_module.stored_record(PROJECT, RUN_ID, reviewed_head_sha=head)[1]


def _dispositions(head: str) -> dict[str, dict[str, object]]:
    record = _stored_record(head) or {}
    stored = record.get(review_module.FINDING_DISPOSITIONS_KEY) or {}
    return {str(index): dict(entry) for index, entry in stored.items()}


def test_a_landed_run_with_a_sub_floor_dimension_is_carried_not_repaired(world):
    """A landed head whose dimension sits below its floor starts no resume."""
    delivered = world["deliver"]("landed-floor")
    scores = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18)
    scores["durability"] = 5
    world["store_review"](head=delivered["head"], base=delivered["base"], scores=scores)
    primary = world["land"](delivered["head"])
    assert primary == delivered["head"]
    before = Path(delivered["manifest"]).read_text(encoding="utf-8")

    report = _dispatch(world)

    assert world["resumed"] == [], "a repair resume was launched for a landed run"
    assert report["dispatched"] is False, report
    assert report["landed"] is True, report
    assert report["integrated"] is True, report
    assert world["dispatched"] == []
    assert report["carried_to"] == {"plan": PLAN_SLUG, "section": SECTION}
    carried_ids = [row["id"] for row in report["carried_findings"]]
    assert carried_ids == ["dimension-durability"], report

    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "carried"
    assert recorded["section"] == SECTION
    assert recorded["dimensions"] == ["durability"]

    # The run's own record is left as it landed: the manifest is untouched and
    # nothing was committed on top of the delivered head.
    assert Path(delivered["manifest"]).read_text(encoding="utf-8") == before
    assert git(world["repo"], "rev-parse", "HEAD") == delivered["head"]


def test_a_landed_run_with_a_blocking_finding_is_carried_not_repaired(world):
    """A landed head with a blocking finding defers it and starts no resume."""
    delivered = world["deliver"]("landed-blocking")
    world["store_review"](
        head=delivered["head"], base=delivered["base"], findings=[BLOCKING]
    )
    world["land"](delivered["head"])

    report = _dispatch(world)

    assert world["resumed"] == [], "a repair resume was launched for a landed run"
    assert report["dispatched"] is False, report
    assert report["landed"] is True, report
    assert world["dispatched"] == []

    dispositions = _dispositions(delivered["head"])
    assert dispositions["0"]["kind"] == "deferred", dispositions
    assert dispositions["0"]["rule"] == "landed-run-carries-undisposed-findings"
    assert dispositions["0"]["section"] == SECTION
    # Deferred, never exempted: the reflex makes no judgement about a finding it
    # did not read, and the finding stays open work a later node answers.
    assert dispositions["0"]["kind"] != "exempted"


def test_an_unlanded_run_with_the_same_review_is_resumed(world):
    """Control: the identical review on an unlanded head is still repaired."""
    delivered = world["deliver"]("unlanded-blocking")
    world["store_review"](
        head=delivered["head"], base=delivered["base"], findings=[BLOCKING]
    )

    report = _dispatch(world)

    assert report.get("resumed") is True, report
    assert len(world["resumed"]) == 1, world["resumed"]
    assert world["dispatched"] == []
    dispositions = _dispositions(delivered["head"])
    assert dispositions.get("0", {}).get("kind") != "deferred", dispositions
