"""A request that cannot launch costs an admission pass nothing.

The pass used to attempt every queued request whose back-off had elapsed, and a
refusal that waiting cannot cure is refused again on every attempt. Each attempt
costs a whole ``dispatch()``: project and plan discovery, then two roadmap
builds. Measured on 28 October, four such requests were released, dispatched and
restored on every pass from 12:05Z to 13:59Z, one of them 54 times, while the
local lane sat idle.

So a request is judged before the launch. A live run's binding claim on one of
its write paths holds it ``held-on-claim`` naming the holder, and it launches on
the first pass after that claim releases. A refusal by the node's own size or
contract holds it ``held-until-changed``, released by the first pass that sees
its plan, brief, route or code change and never by a timer. Every other
failure's back-off is measured from the failure itself, not from the moment the
pass started.

Dispatch attempts are counted through a patched ``_launch_queued``: the measure
is how often a pass reaches the launch, not what the launch answered.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon._timestamps import parse_utc
from reckon.crew import queue_admission, runs, summary
from reckon.crew.node import CompetenceLimit, CrewError
from reckon.crew.refusals import format_refusal
from reckon.crew.summary import Bound
from tests.helpers.git import git as _git
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    SESSION,
    _config_for,
    _lane_document,
    _queue,
    admit_repo,
)

HOLDER_SESSION = "holder-session"


def _dead_pid() -> int:
    """A pid no process holds, so the claim's holder reads as stopped."""
    for candidate in (999999, 999998, 999997, 999996):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
    raise AssertionError("no dead pid available")


def _stopped_holder(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    path: str,
) -> str:
    """Record a stopped run whose worktree still holds the work it claims.

    Its process is gone, so only its unintegrated commit keeps the claim
    binding — the shape the field run takes when a delivered-but-unpromoted run
    fences a path every later dispatch needs.
    """
    base = _git(repository, "rev-parse", "HEAD")
    tree = tmp_path / f"{run_id}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    (tree / path).write_text(f"{path}\n", encoding="utf-8")
    _git(tree, "add", "--", path)
    _git(tree, "commit", "-q", "-m", f"test: hold {path}")
    head = _git(tree, "rev-parse", "HEAD")
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "session": HOLDER_SESSION,
            "phase": "working",
            "worktree": str(tree),
            "base_sha": base,
            "head": head,
            "pid": _dead_pid(),
            "node": {
                "id": run_id,
                "plan": "plan-a",
                "section": "dispatch",
                "time_budget": "20m",
                "write_paths": [path],
            },
        },
    )
    return head


class _LaunchProbe:
    """Count the launches a pass attempts, answering with a bound behaviour.

    ``answer`` is the stand-in for the launch; left ``None`` the probe delegates
    to the real ``_launch_queued``, so an arm that expects a launch still drives
    the shared dispatch path.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempted: list[str] = []
        self.answer: Callable[..., Any] | None = None
        self._real = queue_admission._launch_queued
        monkeypatch.setattr(queue_admission, "_launch_queued", self)

    def __call__(self, pointer, **kwargs):
        self.attempted.append(str(pointer["run_id"]))
        if self.answer is not None:
            return self.answer(pointer, **kwargs)
        return self._real(pointer, **kwargs)


def _competence_limit() -> CompetenceLimit:
    """The refusal a node too large for its lane's window is given."""
    return CompetenceLimit(
        {
            "reason": "context-window-exceeded",
            "backend": "clive",
            "estimated_tokens": 400_000,
            "window_tokens": 262_144,
            "shortfall_tokens": 137_856,
        }
    )


def _capacity_refusal() -> CrewError:
    """The refusal a full lane gives, composed by the emitter that raises it.

    Rendered from a real ``Bound`` through ``summary.bound_refusal_text`` and
    ``format_refusal``, the two calls ``dispatch_admission`` refuses with, so
    the arm reads the production wording rather than a copy that can drift from
    it. Its opening words are the node-contract refusal's, which is the case
    that distinguishes "repair the node" from "wait for a slot".
    """
    bound = Bound(
        name="partition-cores",
        capacity=12.0,
        utilisation=12.0,
        admits_one_more=False,
        value="12 of 12 cores",
    )
    return CrewError(
        format_refusal(
            "D09",
            summary.bound_refusal_text(
                bound, backend_name="clive", occupying=["r-occupant"]
            ),
        )
    )


def _launched(pointer, **_):
    """Stand in for a launch that started: the pass only reads the record's shape."""
    return {"run_id": pointer["run_id"], "phase": "starting"}


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["run_id"]): row for row in result["verdicts"]}


def _move_the_plan(repository: Path) -> None:
    """Change the plan the queued request reads, moving its stamp."""
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.write_text(
        plan.read_text(encoding="utf-8") + "<!-- edited while queued -->\n",
        encoding="utf-8",
    )


def test_a_request_held_by_a_stopped_runs_claim_is_not_dispatched(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim holds it with no attempt, and releases it when it goes."""
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="claimed", run_id="r-claimed"))
    _stopped_holder(admit_repo, tmp_path, run_id="r-holder", path="claimed.txt")
    probe = _LaunchProbe(monkeypatch)
    probe.answer = _launched

    first = queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == [], "a held claim must cost the pass no dispatch"
    assert "r-claimed" in first["held"]
    row = _verdicts(first)["r-claimed"]
    assert row["verdict"] == "held-on-claim", row
    assert "r-holder" in row["detail"], row
    assert runs.read_pointer("r-claimed")["phase"] == "queued"

    # The holder settles: its pointer is gone, so nothing fences the path any
    # more and the very next pass launches the request.
    runs.pointer_path("r-holder").unlink()
    second = queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == ["r-claimed"], "the released request launches at once"
    assert second["launched"] == ["r-claimed"]
    assert _verdicts(second)["r-claimed"]["verdict"] == "launched"


def test_a_competence_refusal_is_held_until_the_request_changes(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal waiting cannot cure is retried only on a change, not a timer."""

    def refuse(pointer, **_):
        raise _competence_limit()

    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="too-big", run_id="r-too-big"))
    probe = _LaunchProbe(monkeypatch)
    probe.answer = refuse

    first = queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == ["r-too-big"]
    assert first["failed"] == ["r-too-big"]
    recorded = runs.read_pointer("r-too-big")
    assert str(recorded["failure"]).startswith("CompetenceLimit:"), recorded["failure"]
    assert recorded["failure_guard"], "the failure must fingerprint what it depends on"

    # Every later pass, far past the back-off the old code honoured, holds it.
    later = datetime.now(UTC) + timedelta(hours=2)
    for _ in range(2):
        held = queue_admission.admit_once(config_for=resolve, now=later)
        assert probe.attempted == ["r-too-big"], "no attempt until the request moves"
        assert "r-too-big" in held["held"]
        assert _verdicts(held)["r-too-big"]["verdict"] == "held-until-changed"

    _move_the_plan(admit_repo)
    released = queue_admission.admit_once(config_for=resolve, now=later)

    assert probe.attempted == ["r-too-big", "r-too-big"], "a moved plan retries at once"
    assert released["failed"] == ["r-too-big"]


def test_a_capacity_refusal_keeps_its_backoff_rather_than_being_held(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full lane is cured by a run finishing, so it is retried on the timer.

    Its opening words are the node-contract refusal's, and the contract refusal
    is held until a change — but nothing a run finishing does moves the
    dependency guard, so holding this one would strand it until the plan moved.
    """

    def refuse(pointer, **_):
        raise _capacity_refusal()

    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="no-room", run_id="r-no-room"))
    probe = _LaunchProbe(monkeypatch)
    probe.answer = refuse

    queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == ["r-no-room"]
    recorded = runs.read_pointer("r-no-room")
    assert "node is not dispatchable" in str(recorded["failure"])
    failed_at = parse_utc(recorded["failure_at"])
    retry_at = parse_utc(recorded["retry_at"])
    assert retry_at - failed_at == timedelta(
        seconds=queue_admission.ADMISSION_RETRY_BACKOFF_SECONDS
    )

    # The back-off expiring is the only cue a capacity hold ever gives, so a
    # pass past it must reach the launch.
    queue_admission.admit_once(config_for=resolve, now=retry_at + timedelta(seconds=1))

    assert probe.attempted == ["r-no-room", "r-no-room"], (
        "a capacity refusal is retried on its back-off, not held until a change"
    )


def test_a_failures_backoff_runs_from_the_failure_not_the_pass_start(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``retry_at`` is the failure's own moment plus the back-off."""

    def refuse(pointer, **_):
        raise ValueError("the lane refused this launch")

    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="broken", run_id="r-broken"))
    probe = _LaunchProbe(monkeypatch)
    probe.answer = refuse
    pass_start = datetime(2020, 1, 1, tzinfo=UTC)

    before = datetime.now(UTC)
    queue_admission.admit_once(config_for=resolve, now=pass_start)
    after = datetime.now(UTC)

    recorded = runs.read_pointer("r-broken")
    failed_at = parse_utc(recorded["failure_at"])
    retry_at = parse_utc(recorded["retry_at"])
    assert before <= failed_at <= after, failed_at
    assert retry_at - failed_at == timedelta(
        seconds=queue_admission.ADMISSION_RETRY_BACKOFF_SECONDS
    )
    assert retry_at - pass_start > timedelta(days=365), (
        "the back-off must not be measured from the pass's start"
    )


def test_a_pass_over_four_requests_attempts_only_the_one_it_can_launch(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three requests held on claims cost one path comparison each, no dispatch."""
    config, resolve = _config_for()
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=10)
    )
    held_ids = []
    for node_id in ("alpha", "beta", "gamma"):
        run_id = f"r-{node_id}"
        runs.queue_dispatch(_queue(admit_repo, node_id=node_id, run_id=run_id))
        _stopped_holder(
            admit_repo, tmp_path, run_id=f"r-holder-{node_id}", path=f"{node_id}.txt"
        )
        held_ids.append(run_id)
    runs.queue_dispatch(_queue(admit_repo, node_id="ready", run_id="r-ready"))
    probe = _LaunchProbe(monkeypatch)
    probe.answer = _launched

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == ["r-ready"], "exactly one request reaches the launch"
    assert result["launched"] == ["r-ready"]
    for run_id in held_ids:
        assert run_id in result["held"]
        assert _verdicts(result)[run_id]["verdict"] == "held-on-claim"
