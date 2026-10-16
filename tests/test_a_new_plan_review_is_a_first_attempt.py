"""A composed plan review is a first attempt, not a redispatch.

Every review that ``crew review --plan <slug>`` composes dispatches under one
node id, ``plan-review-of-<slug>``. ``dispatch()`` reads any earlier committed run
with the same node id as a previous attempt, so a fresh review of a revised plan
was recorded as a redispatch at attempt 2, 3 and higher — a number that means
nothing to a reader who takes it for a retry. A composed plan review now carries
a marker in its dispatch options, and ``dispatch()`` reads it to record a first
attempt: attempt 1, attempt_kind ``dispatch`` and no redispatch lineage. The
marker rides the dispatch options onto the queued record the admission pass
(in ``reckon/crew/queue_admission.py``) replays, so a review the lane holds and
later launches records a first attempt too. Any other node dispatched again
under its own node id keeps the redispatch it records today.

Every case drives the real compose and the real ``dispatch()``: a helper-level
assertion is how the wrong attempt number stayed invisible. Each case seeds the
prior committed run it needs through the ledger, which is the row a promotion
commits, and asserts that row is present where ``dispatch()`` reads it, so a
first-attempt result cannot stand in for a missing precondition.

Declared negative control: with the composed review dispatched as before — the
marker stripped from its dispatch options — the second review records attempt 2
and kind ``redispatch`` and the first case fails.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew, flight, ledger
from reckon.crew import recovery, runs
from reckon.crew.dispatch_admission import LanePaused

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "sample"
PLAN_SLUG = "fixture"
PLAN_VERSION = 3
PLAN_REVIEW_NODE = f"plan-review-of-{PLAN_SLUG}"
IMPL_NODE = "impl-node"
PRIOR_AT = "2026-10-15T00:00:00Z"

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        "implement": {},
        "review": {"backend": "worker", "execution_capable": True},
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN_SLUG}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN_SLUG}">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-version" content="{PLAN_VERSION}">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        "<p>Extend the existing mechanism.</p></body></html>",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", f"docs/plans/{PLAN_SLUG}.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    monkeypatch.setattr(flight, "resolve", lambda **kw: SimpleNamespace(config=CONFIG))
    return home, repo


def _subject():
    return recovery.plan_review_subject(PROJECT, PLAN_SLUG, "coordinator")


def _commit_prior_row(
    repo: Path, node_id: str, *, run_id: str, gate: str = "passed"
) -> None:
    """Commit one ledger row standing for a node's earlier completed run.

    This is the row a promotion writes, and the one ``dispatch()`` reads back
    through ``ledger.runs()`` when it decides a node's attempt number. Seeding it
    directly lets a case state the precondition — an earlier run of this node id
    is committed — without a full promotion of a run the case is not about.
    """
    data, version = ledger.load(PROJECT, root=repo)
    data["runs"].append(
        {
            "run_id": run_id,
            "project": PROJECT,
            "node": node_id,
            "plan": PLAN_SLUG,
            "role": "review" if node_id.startswith("plan-review-of-") else "implement",
            "gate": gate,
            "attempt": 1,
            "lineage": None,
            "created_at": PRIOR_AT,
            "dispatched_at": PRIOR_AT,
            "completed_at": PRIOR_AT,
        }
    )
    ledger.write(PROJECT, data, version, root=repo)
    # Positive control: the precondition is present where dispatch() reads it, so
    # a first-attempt result cannot pass on a seeding that silently did nothing.
    assert any(
        str(row.get("node") or "") == node_id for row in ledger.runs(PROJECT, root=repo)
    ), f"the prior run of {node_id} is not committed where dispatch() reads it"


def _prepare_worktree(tmp_path: Path, head: str):
    def prepare(*_args):
        path = tmp_path / "worktree"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": "HEAD", "base_sha": head}

    return prepare


def _compose_plan_review(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _prepare_worktree(tmp_path, head)
    )
    return recovery.dispatch_review_for_run(_subject(), config=CONFIG)


# ── (1) A second composed review is still a first attempt ─────────────────────


def test_a_plan_review_of_a_revised_plan_is_a_first_attempt(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home, repo = project
    # An earlier review of this same plan was promoted: one committed run under
    # the shared node id, which is exactly what made the next compose read as a
    # redispatch before the marker existed.
    _commit_prior_row(
        repo, PLAN_REVIEW_NODE, run_id=f"r-20261015T000000000000-{PLAN_REVIEW_NODE}"
    )

    payload = _compose_plan_review(repo, monkeypatch, tmp_path)

    assert payload["dispatched"] is True, payload.get("reason")
    launched = payload["dispatch"]
    pointer = runs.read_pointer(payload["review_run_id"])
    assert pointer["node"]["id"] == PLAN_REVIEW_NODE
    assert launched["attempt"] == 1
    assert launched["attempt_kind"] == "dispatch"
    assert not launched.get("lineage")


# ── (2) A review held by the lane is launched as a first attempt ──────────────


def test_a_held_plan_review_records_a_first_attempt_when_admitted(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home, repo = project
    _commit_prior_row(
        repo, PLAN_REVIEW_NODE, run_id=f"r-20261015T000000000000-{PLAN_REVIEW_NODE}"
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    real_dispatch = dispatch_module.dispatch
    captured: dict = {}

    def hold(**kwargs):
        # The lane holds the review: the request is stored as a queued pointer
        # that keeps the dispatch options the compose supplied, which is the
        # record the admission pass later replays.
        captured["options"] = dict(kwargs.get("dispatch_options") or {})
        node = kwargs["node"]
        runs.queue_dispatch(
            {
                "run_id": "queued-review",
                "project": PROJECT,
                "plan": node.plan,
                "section": node.section,
                "repo": str(repo),
                "session": "coordinator",
                "node": node.as_dict(),
                "backend": "worker",
                "launch": "cli",
                "local": True,
                "phase": "queued",
                "queued_at": PRIOR_AT,
                "created_at": PRIOR_AT,
                "reason": "the router grants 0 slots",
                "dispatch_options": dict(kwargs.get("dispatch_options") or {}),
            }
        )
        raise LanePaused({"state": "held", "detail": "the router grants 0 slots"})

    monkeypatch.setattr(dispatch_module, "dispatch", hold)
    held = recovery.dispatch_review_for_run(_subject(), config=CONFIG)
    assert held["dispatched"] is False, held
    assert captured["options"].get("fresh_review") is True

    # The queued request kept the marker, and the admission pass replays it
    # through the real dispatch path.
    queued = runs.read_pointer("queued-review")
    assert queued["dispatch_options"].get("fresh_review") is True

    monkeypatch.setattr(dispatch_module, "dispatch", real_dispatch)
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _prepare_worktree(tmp_path, head)
    )
    admission = importlib.import_module("reckon.crew.queue_admission")
    result = admission.admit_once(
        launcher=lambda *a, **k: 999911, config_for=lambda _pointer: CONFIG
    )
    assert result["launched"], result
    launched_id = str(result["launched"][0])
    pointer = runs.read_pointer(launched_id)
    assert pointer["node"]["id"] == PLAN_REVIEW_NODE
    assert pointer["attempt"] == 1
    assert pointer["attempt_kind"] == "dispatch"
    assert not pointer.get("lineage")


# ── (3) Any other node keeps its redispatch ───────────────────────────────────


def test_a_second_dispatch_of_its_own_node_id_is_a_redispatch(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home, repo = project
    # This node's first run failed; a promotion commits its row.
    _commit_prior_row(
        repo, IMPL_NODE, run_id="r-20261015T000000000000-impl-node-1", gate="failed"
    )

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(
        dispatch_module, "_create_worktree", _prepare_worktree(tmp_path, head)
    )
    brief = tmp_path / "impl-brief.md"
    brief.write_text("Record a second dispatch of this node.\n", encoding="utf-8")
    node = crew.TaskNode(
        id=IMPL_NODE,
        goal="record a second dispatch of this node as a redispatch",
        plan="",
        brief=str(brief),
        role="implement",
        spec_level="guided",
        done_when="pytest tests/test_a_new_plan_review_is_a_first_attempt.py exits 0",
        write_paths=["result.json"],
        time_budget="20m",
    )

    record = crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session="coordinator",
        launcher=lambda *a, **k: 999911,
    )

    assert record["attempt"] == 2
    assert record["attempt_kind"] == "redispatch"
    assert record.get("lineage", {}).get("kind") == "redispatch"
