"""A plan review that queued and was later launched is stored when it delivers.

A plan review's report directory carries ``dispatch.json``, which names the crew
run that ran it. Promotion joins the finished run to the review through that
file. When the compose is queued the sidecar records the request unlaunched —
``status: lane-paused`` and ``run_id: null`` — and the admission pass later
launches it under a new run id. When nothing writes that id back the join finds
no run, so the delivered report is never stored and the plan's build gate stays
shut. Here the admission pass joins the launched run into the composed
directory, the record is stored at promotion with times taken from that run, and
a review that launched directly is joined exactly as before.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import plan_review, recovery, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch_admission import LanePaused
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "sample"
PLAN_SLUG = "fixture"
PLAN_VERSION = 3
LAUNCHED_RUN_ID = "r-20261014T010000000000-plan-review-of-fixture"
LAUNCHED_AT = "2026-10-14T01:00:00+00:00"
COMPLETED_AT = "2026-10-14T01:20:00+00:00"


def _worker_config() -> dict:
    worker = {
        "launch": "cli",
        "command": "codex",
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "time_budget": "20m",
    }
    return {
        "default_backend": "worker",
        "local_backend": "worker",
        "backends": {"worker": worker},
        "roles": {"review": {"backend": "worker", "execution_capable": True}},
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / f"{PLAN_SLUG}.html"
    path.write_text(
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
        ["add", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a committed plan."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    return home, repo


def _compose_while_held(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Compose a plan review held by the lane, leaving one queued pointer.

    Returns the composed report directory, read from the queued pointer before
    the admission pass consumes it.
    """
    dispatch = importlib.import_module("reckon.crew.dispatch")
    subject = recovery.plan_review_subject(PROJECT, PLAN_SLUG, "coordinator")

    def hold(**kwargs):
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
                "queued_at": LAUNCHED_AT,
                "created_at": LAUNCHED_AT,
                "reason": "the router grants 0 slots",
            }
        )
        raise LanePaused({"state": "held", "detail": "the router grants 0 slots"})

    monkeypatch.setattr(dispatch, "dispatch", hold)
    result = recovery.dispatch_review_for_run(subject, config=_worker_config())
    assert result["dispatched"] is False
    return Path(runs.read_pointer("queued-review")["node"]["write_paths"][0])


def _launched_pointer(
    repo: Path, composed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrive at the admission pass with a launch that mints a fresh run id."""
    dispatch = importlib.import_module("reckon.crew.dispatch")
    manifest = repo.parent / f"{LAUNCHED_RUN_ID}.manifest.md"
    manifest.write_text(
        "node: plan-review-of-fixture\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    head = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True
        )
        .stdout.decode()
        .strip()
    )

    def launch(**kwargs):
        _write_json(
            pointer_path(LAUNCHED_RUN_ID),
            {
                "run_id": LAUNCHED_RUN_ID,
                "project": PROJECT,
                "repo": str(repo),
                "worktree": str(repo),
                "base_sha": head,
                "launch": "in-harness",
                "backend": "native",
                "created_at": LAUNCHED_AT,
                "completed_at": COMPLETED_AT,
                "role": "review",
                "manifest_path": str(manifest),
                "node": {
                    "id": f"plan-review-of-{PLAN_SLUG}",
                    "plan": PLAN_SLUG,
                    "section": "",
                    "role": "review",
                    "write_paths": [str(composed)],
                },
            },
        )
        return {"run_id": LAUNCHED_RUN_ID, "phase": "starting"}

    monkeypatch.setattr(dispatch, "dispatch", launch)


def _deliver(composed: Path) -> None:
    (composed / plan_review._REVIEW_REPORT_NAME).write_text(
        "RUBRIC wiring: pass — the plan declares a dependency that resolves.\n",
        encoding="utf-8",
    )


def _stored(composed: Path, repo: Path) -> dict | None:
    committed = review_module.committed_review_root(PROJECT, root=repo)
    if committed is None:
        return None
    path = plan_review.plan_review_path(
        PROJECT,
        PLAN_SLUG,
        PLAN_VERSION,
        committed_root=committed,
        review_run_id=composed.name,
    )
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ── (1) A queued review is joined, delivered and stored at promotion ──────────


def test_a_queued_plan_review_is_stored_when_it_delivers(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, repo = project
    composed = _compose_while_held(repo, monkeypatch)
    composed_run_id = composed.name

    # The compose recorded the request unlaunched: the sidecar names no run.
    sidecar_path = composed / review_module.REVIEW_DISPATCH_FILE_NAME
    assert json.loads(sidecar_path.read_text(encoding="utf-8"))["run_id"] is None

    _launched_pointer(repo, composed, monkeypatch)
    admission = importlib.import_module("reckon.crew.queue_admission")
    result = admission.admit_once(config_for=lambda _pointer: _worker_config())

    assert result["launched"] == [LAUNCHED_RUN_ID]
    # The admission pass wrote the launched run id back into the sidecar, so
    # promotion's join resolves instead of finding a null run id.
    assert (
        review_module._plan_review_crew_run_id(PROJECT, PLAN_SLUG, composed_run_id)
        == LAUNCHED_RUN_ID
    )

    _deliver(composed)
    crew.complete(LAUNCHED_RUN_ID, gate="passed", root=repo)

    stored = _stored(composed, repo)
    assert stored is not None
    assert stored["review_run_id"] == composed_run_id
    # The dispatch stamp is the launched run's own created_at, not the queued
    # compose's, and the times are taken from that run's record.
    assert stored.get("dispatched_at") == LAUNCHED_AT
    assert stored.get("completed_at")
    assert stored.get("times_source") == review_module.RUN_RECORD_TIMES_SOURCE


# ── (2) A review that launched directly is still stored at promotion ──────────


def test_a_directly_launched_plan_review_is_still_stored(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, repo = project
    composed = _compose_while_held(repo, monkeypatch)
    composed_run_id = composed.name

    _launched_pointer(repo, composed, monkeypatch)
    admission = importlib.import_module("reckon.crew.queue_admission")
    admission.admit_once(config_for=lambda _pointer: _worker_config())

    # A direct launch writes the run id into the sidecar itself, exactly as the
    # compose path does; the join resolves from the same field.
    (composed / review_module.REVIEW_DISPATCH_FILE_NAME).write_text(
        json.dumps({"run_id": LAUNCHED_RUN_ID, "status": "dispatched"}),
        encoding="utf-8",
    )
    _deliver(composed)
    crew.complete(LAUNCHED_RUN_ID, gate="passed", root=repo)

    stored = _stored(composed, repo)
    assert stored is not None
    assert stored["review_run_id"] == composed_run_id
