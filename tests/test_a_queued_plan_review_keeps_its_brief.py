"""A queued plan review keeps the brief its queued pointer will read.

A plan-review attempt composes its run directory — the brief, the plan snapshot
and the sidecar — before the lane admits it. When the lane holds the session
the request is queued: a pointer is written naming the brief inside that
directory, and the pointer launches from the brief once a slot frees. The
dispatch report that comes back from that hold does not always carry the
``queued`` key, so a discard that removes the directory whenever the report
lacks it takes the brief out from under a request that will later read it.
Here the brief is kept for the request the pointer names, judged from the
pointer itself rather than the shape of the report, and the admitted launch
reads the brief the pointer kept.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import plan_review, recovery, runs


def _config() -> dict:
    worker = {
        "launch": "cli",
        "command": "codex",  # never launched here: dispatch is replaced below
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
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    path = plans / "fixture.html"
    path.write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
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
    (home / "mounts.json").write_text(json.dumps({"sample": str(repo / "docs")}))
    return home, repo, path


def _subject(**kwargs):
    return recovery.plan_review_subject("sample", "fixture", "coordinator", **kwargs)


def test_a_queued_pointer_keeps_the_brief_the_admitted_launch_reads(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, repo, _path = project
    dispatch = importlib.import_module("reckon.crew.dispatch")
    admission = importlib.import_module("reckon.crew.queue_admission")
    subject = _subject()
    directory = plan_review.review_report_directory(
        "sample", "fixture", subject["run_id"]
    )

    def hold(**kwargs):
        node = kwargs["node"]
        runs.queue_dispatch(
            {
                "run_id": "queued-review",
                "project": "sample",
                "plan": node.plan,
                "section": node.section,
                "repo": str(repo),
                "session": "coordinator",
                "node": node.as_dict(),
                "backend": "worker",
                "launch": "cli",
                "local": True,
                "phase": "queued",
                "queued_at": "2026-10-13T00:00:00+00:00",
                "created_at": "2026-10-13T00:00:00+00:00",
                "reason": "the router grants 0 slots",
            }
        )
        # The lane-paused refusal a held compose returns does not set the
        # report's ``queued`` key: only the pointer on disk names the brief.
        raise admission.LanePaused({"state": "held", "detail": "the router grants 0 slots"})

    monkeypatch.setattr(dispatch, "dispatch", hold)
    result = recovery.dispatch_review_for_run(subject, config=_config())

    assert result["dispatched"] is False
    assert result["error"] == "lane-paused"
    assert "queued" not in result
    assert (directory / "brief.md").is_file()

    queued = runs.list_live(project="sample", phase="queued")
    assert len(queued) == 1
    pointer = queued[0]
    brief_path = Path(pointer["node"]["brief"])
    assert brief_path == directory / "brief.md"
    assert brief_path.is_file()

    read: dict[str, str] = {}

    def launcher(**kwargs):
        read["brief"] = Path(kwargs["node"].brief).read_text(encoding="utf-8")
        return {"run_id": "launched-review"}

    monkeypatch.setattr(dispatch, "dispatch", launcher)
    launched = admission._launch_queued(
        pointer, launcher=None, config_for=lambda _pointer: _config()
    )

    assert launched["run_id"] == "launched-review"
    assert read["brief"] == (directory / "brief.md").read_text(encoding="utf-8")
    assert read["brief"].strip()