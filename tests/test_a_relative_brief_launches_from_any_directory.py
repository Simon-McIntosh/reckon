"""A brief named by a relative path resolves for every launcher.

A coordinator dispatches from the repository root, so ``--brief briefs/note.md``
reads there. The admit service launches a queued request from whatever directory
the service was started in, and a path stored as the coordinator typed it
resolves against nothing there. The request then refuses its launch on every
pass and reads queued.

A request that keeps failing to launch is not retried for ever either: after the
configured number of failed launches in a row it stays ``launch-failed``,
carrying the last failure, where its coordinator can see it.
"""

# ruff: noqa: F811 - the fixtures are imported and also named as test parameters.

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_admission import CrewError
from reckon.crew.dispatch_plan import plan_dispatch
from reckon.crew.node import TaskNode
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixtures
    _config_for,
    _lane_document,
    _queue,
    admit_repo,
)

PROJECT = "proj"
BRIEF_TEXT = "The brief this node was dispatched with, from a relative path.\n"
RELATIVE_BRIEF = "briefs/note.md"


def _seed_brief(repository: Path, monkeypatch) -> Path:
    """Write the brief into the repository, then stand in the repository root."""
    brief = repository / "briefs" / "note.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "--", RELATIVE_BRIEF],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "commit",
            "-q",
            "-m",
            "docs: seed the brief",
            "-m",
            "Give the dispatch a relative path the repository carries.",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(repository)
    return brief


def _node(brief: str) -> TaskNode:
    return TaskNode(
        id="briefed",
        goal="read the brief this node was dispatched with",
        plan="plan-a",
        section="dispatch",
        brief=brief,
        done_when="pytest reports the brief resolved",
        write_paths=["briefed.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _plan(repository: Path, brief: str):
    return plan_dispatch(
        node=_node(brief),
        config=_config_for()[0],
        project=PROJECT,
        repo=repository,
        route="deterministic",
        allow_unreviewed_plan=True,
    )


def test_a_relative_brief_resolves_against_the_repository(
    admit_repo: Path, monkeypatch, tmp_path: Path
) -> None:
    """The stored path is absolute, so a launcher elsewhere reads the same file."""
    brief = _seed_brief(admit_repo, monkeypatch)

    plan = _plan(admit_repo, RELATIVE_BRIEF)

    resolved = Path(plan.node.brief)
    assert resolved.is_absolute()
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert resolved.resolve() == brief.resolve()
    stored = runs.store_brief(runs.run_dir("r-elsewhere"), resolved)
    assert stored.read_text(encoding="utf-8") == BRIEF_TEXT


def test_a_missing_brief_is_refused_rather_than_stored(
    admit_repo: Path, monkeypatch
) -> None:
    """A brief that is absent is refused at dispatch, before anything is stored."""
    monkeypatch.chdir(admit_repo)

    with pytest.raises(CrewError) as refused:
        _plan(admit_repo, "briefs/absent.md")

    assert "briefs/absent.md" in str(refused.value)


def test_a_queued_request_launches_from_an_unrelated_directory(
    admit_repo: Path, monkeypatch, tmp_path: Path
) -> None:
    """The launch, run outside the repository, reads the brief it was queued with."""
    _seed_brief(admit_repo, monkeypatch)
    request = _queue(admit_repo, node_id="briefed", run_id="r-briefed")
    request["node"]["brief"] = RELATIVE_BRIEF
    runs.queue_dispatch(request)
    # What a queued request looks like once an earlier attempt's run directory
    # went with its copy: the source the request names is what must be read.
    runs.run_dir("r-briefed").joinpath(runs.STEERING_FILE).unlink()

    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    captured = {}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        captured["prompt"] = plan.prompt
        captured["cwd"] = Path(plan.cwd)
        return os.getpid()

    monkeypatch.chdir(elsewhere)
    result = queue_admission.admit_once(
        config_for=_config_for()[1], launcher=launcher, lock=True
    )

    assert result["failed"] == [], runs.read_pointer("r-briefed").get("failure")
    assert "r-briefed" in result["launched"]
    assert captured["cwd"].resolve() != elsewhere.resolve()
    assert BRIEF_TEXT in captured["prompt"]
    stored = runs.run_dir("r-briefed") / "brief.md"
    assert BRIEF_TEXT in stored.read_text(encoding="utf-8")


def _launch_pointer(repository: Path, attempts: int) -> None:
    """Write a run the reaper marked launch-failed after ``attempts`` failures."""
    pointer = _queue(repository, node_id="reaped", run_id="r-reaped")
    pointer["phase"] = "launch-failed"
    pointer["launch_failures"] = [
        {
            "kind": "launch-failed",
            "recorded_at": "2026-10-12T05:00:00+00:00",
            "stderr_tail": "the worker command was not found",
        }
    ]
    admitted = _queue(repository, node_id="reaped", run_id="r-reaped")
    admitted["failure_attempts"] = attempts
    pointer["admit_request"] = admitted
    runs._write_json(runs.pointer_path("r-reaped"), pointer)


def test_a_request_that_failed_to_launch_too_often_stays_launch_failed(
    admit_repo: Path, tmp_path: Path
) -> None:
    """The configured number of failed launches in a row stops the re-queue."""
    config, resolve = _config_for()
    config["queue"] = {"launch_failure_limit": 2}
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=0)
    )
    # One launch has already failed and been re-queued, so this failure is the
    # configured second one in a row.
    _launch_pointer(admit_repo, 1)

    result = queue_admission.admit_once(config_for=resolve, lock=True)

    assert "r-reaped" not in result["requeued"]
    kept = runs.read_pointer("r-reaped")
    assert kept["phase"] == "launch-failed"
    assert kept["launch_failures"][-1]["kind"] == "launch-failed"
    assert "the worker command was not found" in str(kept.get("failure") or "")


def test_a_request_below_the_limit_is_requeued(
    admit_repo: Path, tmp_path: Path
) -> None:
    """One failed launch in a row is below the limit, so the request re-queues."""
    config, resolve = _config_for()
    config["queue"] = {"launch_failure_limit": 2}
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=0)
    )
    _launch_pointer(admit_repo, 0)

    result = queue_admission.admit_once(config_for=resolve, lock=True)

    assert "r-reaped" in result["requeued"]
    assert runs.read_pointer("r-reaped")["phase"] == "queued"
