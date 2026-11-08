"""A launch refused on a write claim returns its request to the queue.

A queued request is checked against live claims before it is launched, but a
claim can be taken in the window between that check and the launch itself. The
launch then raises ``ScopeConflict`` after the pointer has been published in
phase ``starting``. Recording the failure in place left the pointer in
``starting`` with no attempt behind it: on 8 November a request sat that way for
55 minutes, carried by no reaper and drawn as halted, until a coordinator
discarded it.

The refusal is a hold like any other: the request goes back to ``queued``,
carrying the claim that refused it, and the next pass launches it once that
claim is released. The tests here also read the first half of the same rule —
a stopped run whose worktree still holds an unlanded commit keeps binding, and
its claim holds the request before any dispatch is spent on it.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.node import ScopeConflict, TaskNode
from tests.helpers.git import git as _git
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    SESSION,
    _config_for,
    admit_repo,
)

HOLDER_ID = "r-holder"
REQUEST_ID = "r-queued"
CLAIMED = "claimed.txt"


def _dead_pid() -> int:
    """A pid no process holds, so the holder reads as a stopped run."""
    for candidate in (999996, 999997, 999998, 999999):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
    raise AssertionError("no dead pid available")


def _stopped_holder(repository: Path, tmp_path: Path) -> str:
    """Record a stopped run whose worktree still holds the path it claims.

    The commit keeps the claim binding after the process is gone, which is the
    shape a delivered-but-unpromoted run takes when it fences a later request.
    """
    base = _git(repository, "rev-parse", "HEAD")
    tree = tmp_path / f"{HOLDER_ID}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    (tree / CLAIMED).write_text(f"{CLAIMED}\n", encoding="utf-8")
    _git(tree, "add", "--", CLAIMED)
    _git(tree, "commit", "-q", "-m", f"test: hold {CLAIMED}")
    head = _git(tree, "rev-parse", "HEAD")
    runs._write_json(
        runs.pointer_path(HOLDER_ID),
        {
            "run_id": HOLDER_ID,
            "project": PROJECT,
            "repo": str(repository),
            "session": SESSION,
            "phase": "working",
            "worktree": str(tree),
            "base_sha": base,
            "head": head,
            "pid": _dead_pid(),
            "node": {
                "id": HOLDER_ID,
                "plan": "plan-a",
                "section": "dispatch",
                "time_budget": "20m",
                "write_paths": [CLAIMED],
            },
        },
    )
    return head


def _queue_request(repository: Path) -> None:
    node = TaskNode(
        id="queued",
        goal="write the claimed path",
        plan="plan-a",
        section="dispatch",
        done_when="pytest reports passed",
        write_paths=[CLAIMED],
        time_budget="20m",
        spec_level="exact",
    )
    joined = datetime.now(UTC).isoformat()
    runs.queue_dispatch(
        {
            "run_id": REQUEST_ID,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "node": node.as_dict(),
            "backend": "clive",
            "launch": "cli",
            "local": True,
            "phase": "queued",
            "queued_at": joined,
            "created_at": joined,
            "hold": {"held": True},
            "dispatch_options": {
                "project": PROJECT,
                "plan_slug": "plan-a",
                "section": "dispatch",
                "session": SESSION,
                "local": True,
                "no_watch": True,
                "overrides": [],
            },
        }
    )


class _LaunchProbe:
    """Stand in for the launch, counting the attempts a pass makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempted: list[str] = []
        monkeypatch.setattr(queue_admission, "_launch_queued", self)

    def __call__(self, pointer: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.attempted.append(str(pointer["run_id"]))
        return {"run_id": pointer["run_id"], "phase": "starting"}


class _ClaimTakenAtLaunch:
    """Publish the launch's pointer, then be refused by a claim just taken.

    The dispatch path writes the request's pointer before it checks the claim it
    is about to write under, and a claim registered in that window refuses the
    launch. This stands in for exactly that race: the pointer is in phase
    ``starting`` and the launch raises the refusal the real check raises.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, claimed: str) -> None:
        self.claimed = claimed
        self.attempted: list[str] = []
        monkeypatch.setattr(queue_admission, "_launch_queued", self)

    def __call__(self, pointer: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.attempted.append(str(pointer["run_id"]))

        def starting(current: dict[str, Any]) -> dict[str, Any]:
            current["phase"] = "starting"
            return current

        runs._mutate_pointer(str(pointer["run_id"]), starting)
        raise ScopeConflict(
            run_id=HOLDER_ID,
            node_id=HOLDER_ID,
            candidate_path=self.claimed,
            claimed_path=self.claimed,
        )


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["run_id"]): row for row in result["verdicts"]}


def test_a_stopped_holders_claim_holds_the_request_before_launch(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped run holding an unlanded commit fences the request first."""
    _, resolve = _config_for()
    _queue_request(admit_repo)
    _stopped_holder(admit_repo, tmp_path)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == [], "a held claim must cost the pass no dispatch"
    assert REQUEST_ID in result["held"]
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in row["detail"], row
    assert CLAIMED in row["detail"], row
    pointer = runs.read_pointer(REQUEST_ID)
    assert pointer["phase"] == "queued"
    assert not pointer.get("failure")


def test_a_claim_taken_at_launch_returns_the_request_to_the_queue(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal raised by the launch itself leaves no starting pointer behind."""
    _, resolve = _config_for()
    _queue_request(admit_repo)
    launch = _ClaimTakenAtLaunch(monkeypatch, claimed=CLAIMED)

    result = queue_admission.admit_once(config_for=resolve)

    assert launch.attempted == [REQUEST_ID]
    assert result["launched"] == []
    pointer = runs.read_pointer(REQUEST_ID)
    assert pointer["phase"] == "queued", pointer
    assert pointer.get("pid") is None
    hold = queue_admission.recorded_pass_hold(pointer)
    assert hold["verdict"] == "refused-scope-conflict", hold
    assert hold["holder"]["run_id"] == HOLDER_ID, hold
    assert hold["holder"]["path"] == CLAIMED, hold


def test_the_request_launches_once_the_holders_commits_reach_main(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold releases with the claim, and the next pass launches."""
    _, resolve = _config_for()
    _queue_request(admit_repo)
    head = _stopped_holder(admit_repo, tmp_path)
    held = _LaunchProbe(monkeypatch)

    queue_admission.admit_once(config_for=resolve)
    assert held.attempted == []

    _git(admit_repo, "merge", "--ff-only", head)
    launched = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert launched.attempted == [REQUEST_ID]
    assert result["launched"] == [REQUEST_ID]
    assert _verdicts(result)[REQUEST_ID]["verdict"] == "launched"


def test_the_real_crew_home_is_untouched(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every record this test writes lives under the temporary crew home."""
    _, resolve = _config_for()
    real_home = Path.home() / ".config" / "reckon" / "crew"
    before = sorted(path.name for path in (real_home / "live").glob("*.json"))
    _queue_request(admit_repo)
    launch = _ClaimTakenAtLaunch(monkeypatch, claimed=CLAIMED)

    queue_admission.admit_once(config_for=resolve)

    assert launch.attempted == [REQUEST_ID]
    assert runs.crew_home() != real_home
    after = sorted(path.name for path in (real_home / "live").glob("*.json"))
    assert after == before
    assert not (real_home / "live" / f"{REQUEST_ID}.json").exists()
