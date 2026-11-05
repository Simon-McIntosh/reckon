"""A claim hold reads its holder again before it is recorded.

An admission pass reads the live set once, then judges every queued request
against that one reading. The launch stage of a busy pass can run for an hour,
so a holder that landed while the pass ran kept fencing its peers until the
next pass began: on 5 November one landed run held eight requests an hour
after it had landed, and two further holders had no live pointer at all.

The re-read costs one pointer read per hold, not a fresh read of the live set,
so the second case here is the base behaviour exactly as it was.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs
from tests.helpers.git import git as _git
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    SESSION,
    _config_for,
    _queue,
    admit_repo,
)

HOLDER_ID = "r-holder"
REQUEST_ID = "r-late"
CLAIMED = "claimed.txt"


def _dead_pid() -> int:
    """A pid no process holds, so the holder reads as a stopped run."""
    for candidate in (999996, 999997, 999998, 999999):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
    raise AssertionError("no dead pid available")


def _stopped_holder(repository: Path, tmp_path: Path) -> None:
    """Record a stopped run whose worktree still holds the path it claims.

    Its process is gone, so only the unintegrated commit keeps the claim
    binding — the shape a delivered-but-unpromoted run takes when it fences the
    paths a later dispatch needs.
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


def _remove_the_holder_after_the_live_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> list[bool]:
    """Take the pass's live reading, then unlink the holder's pointer.

    The pass reads the live set once and judges requests against it for the
    rest of the stage. Removing the holder between that reading and the
    judgement is the state a long pass reaches when its holder lands.
    """
    real = runs.list_live
    fired: list[bool] = []

    def reading(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        records = real(*args, **kwargs)
        if not fired and not args and not kwargs:
            fired.append(True)
            runs.pointer_path(HOLDER_ID).unlink()
        return records

    monkeypatch.setattr(runs, "list_live", reading)
    return fired


class _LaunchProbe:
    """Stand in for the launch, counting the attempts a pass makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.attempted: list[str] = []
        monkeypatch.setattr(queue_admission, "_launch_queued", self)

    def __call__(self, pointer, **_):
        self.attempted.append(str(pointer["run_id"]))
        return {"run_id": pointer["run_id"], "phase": "starting"}


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["run_id"]): row for row in result["verdicts"]}


def test_a_holder_that_landed_before_the_judgement_does_not_hold(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder's pointer is gone by the judgement, so nothing fences it."""
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="claimed", run_id=REQUEST_ID))
    _stopped_holder(admit_repo, tmp_path)
    fired = _remove_the_holder_after_the_live_reading(monkeypatch)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert fired, "the pass must have taken its live reading before the removal"
    assert runs.pointer_path(HOLDER_ID).exists() is False
    assert probe.attempted == [REQUEST_ID], (
        "a holder that landed must not fence a peer that was judged after it"
    )
    assert result["launched"] == [REQUEST_ID]
    assert _verdicts(result)[REQUEST_ID]["verdict"] == "launched"


def test_a_holder_that_still_claims_the_path_still_holds_the_request(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder whose pointer is still there holds it exactly as before."""
    _, resolve = _config_for()
    runs.queue_dispatch(_queue(admit_repo, node_id="claimed", run_id=REQUEST_ID))
    _stopped_holder(admit_repo, tmp_path)
    probe = _LaunchProbe(monkeypatch)

    result = queue_admission.admit_once(config_for=resolve)

    assert probe.attempted == [], "a held claim must cost the pass no dispatch"
    assert REQUEST_ID in result["held"]
    row = _verdicts(result)[REQUEST_ID]
    assert row["verdict"] == "held-on-claim", row
    assert HOLDER_ID in row["detail"], row
    assert runs.read_pointer(REQUEST_ID)["phase"] == "queued"
