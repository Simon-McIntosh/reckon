"""Every launch of a queued plan review joins the composed sidecar.

A composed plan review keeps its ``dispatch.json`` naming the crew run that ran
it, and promotion joins the delivered report to the review through that file. A
review composed while the lane holds records the sidecar unlaunched — ``status:
lane-paused``, ``run_id: null``. Every path that later launches that request
must write the launched run id back, or promotion finds no run and the review is
never stored.

Three launches reach the dispatch through ``queue_admission._launch_queued``:
the admission pass's own pass over the queue, a dispatch that yields its lane
slot to an older request of its session, and a queued request moved onto another
backend. The sidecar names the launched run on each, and the delivered report is
stored when the launched run promotes.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
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
QUEUED_RUN_ID = "queued-review"
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
        "backends": {"worker": worker, "other": dict(worker)},
        "roles": {
            "review": {
                "backend": "worker",
                "execution_capable": True,
                "sandbox": "worktree-full",
            }
        },
        "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    flight = home / "flight.json"
    flight.write_text(json.dumps(_worker_config()), encoding="utf-8")
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(flight))
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
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a commit."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    return home, repo


def _compose_while_held(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Compose a plan review held by the lane, leaving one queued pointer."""
    dispatch = importlib.import_module("reckon.crew.dispatch")
    subject = recovery.plan_review_subject(PROJECT, PLAN_SLUG, "coordinator")

    def hold(**kwargs):
        node = kwargs["node"]
        runs.queue_dispatch(
            {
                "run_id": QUEUED_RUN_ID,
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
    return Path(runs.read_pointer(QUEUED_RUN_ID)["node"]["write_paths"][0])


def _launch_returns_a_new_run(
    repo: Path, composed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make dispatch return a launched run for the queued review."""
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


def _assert_joined_and_stored(composed: Path, repo: Path) -> None:
    assert (
        review_module._plan_review_crew_run_id(PROJECT, PLAN_SLUG, composed.name)
        == LAUNCHED_RUN_ID
    ), "the composed sidecar does not name the run that launched it"

    (composed / plan_review._REVIEW_REPORT_NAME).write_text(
        "RUBRIC reuse_search: pass — the plan reuses the existing owner.\n",
        encoding="utf-8",
    )
    crew.complete(LAUNCHED_RUN_ID, gate="passed", root=repo)

    committed = review_module.committed_review_root(PROJECT, root=repo)
    assert committed is not None, "the promotion committed no review store"
    path = plan_review.plan_review_path(
        PROJECT,
        PLAN_SLUG,
        PLAN_VERSION,
        committed_root=committed,
        review_run_id=composed.name,
    )
    assert path.is_file(), "the delivered review was never stored"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["review_run_id"] == composed.name


def _queued_and_composed(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    _home, repo = project
    composed = _compose_while_held(repo, monkeypatch)
    sidecar = composed / review_module.REVIEW_DISPATCH_FILE_NAME
    assert json.loads(sidecar.read_text(encoding="utf-8"))["run_id"] is None
    _launch_returns_a_new_run(repo, composed, monkeypatch)
    return repo, composed


# ── Path 1: a dispatch yields its lane slot to the queued review ─────────────


def test_the_yield_path_joins_the_composed_sidecar(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, composed = _queued_and_composed(project, monkeypatch)
    dispatch = importlib.import_module("reckon.crew.dispatch")
    pointer = runs.read_pointer(QUEUED_RUN_ID)
    launched = dispatch._launch_yielded_queued_request(
        pointer, launcher=None, moment=datetime.now(UTC)
    )
    assert launched is not None and launched["run_id"] == LAUNCHED_RUN_ID
    _assert_joined_and_stored(composed, repo)


# ── Path 2: a queued review moved onto another backend ───────────────────────


def test_the_moved_path_joins_the_composed_sidecar(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, composed = _queued_and_composed(project, monkeypatch)
    admission = importlib.import_module("reckon.crew.queue_admission")
    moved = admission.move_queued_run(
        QUEUED_RUN_ID,
        "other",
        reason="the lane refuses the request's size",
        config=_worker_config(),
    )
    assert str(moved["run_id"]) == LAUNCHED_RUN_ID
    _assert_joined_and_stored(composed, repo)


# ── Path 3: the admission pass ───────────────────────────────────────────────


def test_the_admission_pass_joins_the_composed_sidecar(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, composed = _queued_and_composed(project, monkeypatch)
    admission = importlib.import_module("reckon.crew.queue_admission")
    result = admission.admit_once(config_for=lambda _pointer: _worker_config())
    assert result["launched"] == [LAUNCHED_RUN_ID]
    _assert_joined_and_stored(composed, repo)


# ── A dispatch that returns the request still queued joins nothing ───────────


def test_a_queued_record_writes_no_sidecar(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo, composed = _queued_and_composed(project, monkeypatch)
    dispatch = importlib.import_module("reckon.crew.dispatch")
    monkeypatch.setattr(
        dispatch,
        "dispatch",
        lambda **_kwargs: {"run_id": QUEUED_RUN_ID, "phase": "queued"},
    )
    admission = importlib.import_module("reckon.crew.queue_admission")
    pointer = runs.read_pointer(QUEUED_RUN_ID)

    returned = admission._launch_queued(
        pointer, launcher=None, config_for=lambda _pointer: _worker_config()
    )

    assert returned["phase"] == "queued"
    sidecar = composed / review_module.REVIEW_DISPATCH_FILE_NAME
    assert json.loads(sidecar.read_text(encoding="utf-8"))["run_id"] is None
