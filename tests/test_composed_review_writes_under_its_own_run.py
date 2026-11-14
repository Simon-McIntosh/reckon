"""A composed plan review writes its record where its own promotion reads it.

A plan review is composed under a review id its report directory is named for,
and its brief names the report path under that directory. The crew run that
actually reads the plan is launched under a different id, so the run's own id
does not name the record — the directory's ``dispatch.json`` does.

Here a stub reviewer writes exactly where the brief says, the run ends, and the
review's own promotion is asked for the record. Promotion reads the composed
directory the brief named rather than a directory named for the run id, so the
record is found and stored. The negative control makes promotion read only the
run id, and the run's promotion then refuses for a record it does not find.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import plan_review, recovery, runs
from reckon.crew import review as review_module
from reckon.crew.dispatch_admission import LanePaused
from reckon.crew.recovery_review_acceptance import promote_review_run_itself
from reckon.crew.runs import _write_json, pointer_path, run_dir
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


def _compose_directly(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Compose a plan review whose dispatch returns a launched run at once.

    No queued pointer is written and the admission pass never runs, so the
    run id in the sidecar can only come from the compose path itself.
    """
    subject = recovery.plan_review_subject(PROJECT, PLAN_SLUG, "coordinator")
    composed = plan_review.review_report_directory(
        PROJECT, PLAN_SLUG, subject["run_id"]
    )
    _launched_pointer(repo, composed, monkeypatch)
    result = recovery.dispatch_review_for_run(subject, config=_worker_config())
    assert result["dispatched"] is True
    return composed


def _deliver(composed: Path) -> None:
    (composed / plan_review._REVIEW_REPORT_NAME).write_text(
        "RUBRIC reuse_search: pass — the plan reuses the existing owner.\n",
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


def _brief_report_path(composed: Path) -> Path:
    """The report path the composed brief names, read from the brief itself."""
    text = (composed / "brief.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("Report path:"):
            return Path(line.split(":", 1)[1].strip())
    raise AssertionError("the composed brief names no report path")


def _end_the_review_run() -> dict:
    """Record the supervisor exit a review's own promotion waits for."""
    _write_json(
        run_dir(LAUNCHED_RUN_ID) / "exit.json",
        {"run_id": LAUNCHED_RUN_ID, "exit_code": 0, "ended_at": COMPLETED_AT},
    )
    return json.loads(pointer_path(LAUNCHED_RUN_ID).read_text(encoding="utf-8"))


def test_a_composed_plan_review_promotes_the_record_its_brief_named(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, repo = project
    composed = _compose_directly(repo, monkeypatch)
    report = _brief_report_path(composed)
    assert report.parent == composed, (report, composed)
    _deliver(composed)
    assert report.is_file()
    pointer = _end_the_review_run()

    promotion = promote_review_run_itself(pointer)

    assert promotion["promoted"] is True, promotion
    assert _stored(composed, repo) is not None


def test_negative_control_promotion_reading_only_the_run_id_refuses(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, repo = project
    composed = _compose_directly(repo, monkeypatch)
    _deliver(composed)
    pointer = _end_the_review_run()
    acceptance = importlib.import_module("reckon.crew.recovery_review_acceptance")
    monkeypatch.setattr(
        acceptance,
        "_plan_review_delivery",
        lambda record, project_name: None,
    )

    promotion = promote_review_run_itself(pointer)

    assert promotion["promoted"] is False, promotion
    assert "stored no readable record" in promotion["reason"], promotion["reason"]
