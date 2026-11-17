"""A queued request waits behind an earlier one only over a path git cannot merge.

The concurrent-edit rule lets two workers edit one source file and leaves git to
reconcile them at the merge, so the admission pass must not serialise source
paths: a request that declares a directory such as ``reckon`` would otherwise
park everything queued behind it. The pre-check therefore holds only over the
class promotion and the write-time audit refuse — a plan document, generated
state, a cumulative evidence record, a figure topic directory or a dependency
lock.

The checkout, lane document, request builder and launcher are the ones the
overlapping-queue suite already builds, so a change to the shape of a queued
request is made in one place rather than two.
"""

# ruff: noqa: F811 - the fixtures are imported and also named as test parameters.

from __future__ import annotations

from pathlib import Path

from reckon.crew import queue_admission, runs
from tests.test_a_queued_run_is_admitted import admit_repo  # noqa: F401 - fixture
from tests.test_overlapping_queued_requests_launch_in_turn import (
    SESSION_A,
    SESSION_B,
    START,
    _config,
    _lane_document,
    _launcher,
    _queue,
)

# Two wide declarations a node is routinely granted. Neither is a path a merge
# cannot reconcile: both trees hold source text the worktrees keep apart.
WIDE_PATHS = ["reckon", "tests"]
# The plan document is one classification of the directories above.
PLAN = "docs/plans/plan-a.html"


def _two_slots(tmp_path: Path) -> dict:
    return _config(_lane_document(tmp_path, slots={SESSION_A: 1, SESSION_B: 1}))


def test_two_requests_over_wide_source_directories_are_both_admitted(
    admit_repo: Path, tmp_path: Path
) -> None:
    """One pass with two free slots launches both, in rank order."""
    config = _two_slots(tmp_path)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="earlier",
            run_id="r-earlier",
            session=SESSION_A,
            write_paths=list(WIDE_PATHS),
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="later",
            session=SESSION_B,
            run_id="r-later",
            write_paths=list(WIDE_PATHS),
            queued_at="2026-10-14T11:59:00+00:00",
        )
    )
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert result["held"] == [], result["verdicts"]
    assert len(result["launched"]) == 2, result["launched"]
    assert len(recorded) == 2
    assert {
        runs.read_pointer(run_id)["node"]["id"] for run_id in result["launched"]
    } == {
        "earlier",
        "later",
    }
    assert {runs.read_pointer(run_id)["phase"] for run_id in result["launched"]} != {
        "queued"
    }


def test_two_requests_over_one_plan_document_are_held_in_rank_order(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A plan document is still one writer at a time, so the later one waits."""
    config = _two_slots(tmp_path)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="earlier",
            run_id="r-earlier",
            session=SESSION_A,
            write_paths=[PLAN],
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="later",
            run_id="r-later",
            session=SESSION_B,
            write_paths=[PLAN],
            queued_at="2026-10-14T11:59:00+00:00",
        )
    )
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert result["held"] == ["r-later"], result["held"]
    assert len(result["launched"]) == 1
    assert runs.read_pointer(result["launched"][0])["node"]["id"] == "earlier"
    later = runs.read_pointer("r-later")
    assert later["phase"] == "queued"
    assert not later.get("failure")


def test_a_wide_directory_still_holds_over_a_plan_document_beneath_it(
    admit_repo: Path, tmp_path: Path
) -> None:
    """A broad claim does not walk past the exclusive file it covers."""
    config = _two_slots(tmp_path)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="earlier",
            run_id="r-earlier",
            session=SESSION_A,
            write_paths=[PLAN],
            queued_at="2026-10-14T11:58:00+00:00",
        )
    )
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id="later",
            session=SESSION_B,
            run_id="r-later",
            write_paths=["docs"],
            queued_at="2026-10-14T11:59:00+00:00",
        )
    )
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert result["held"] == ["r-later"], result["held"]
    assert runs.read_pointer("r-later")["phase"] == "queued"
