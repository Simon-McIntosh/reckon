"""A launch adopts the reservation its own door took for an attempt that never ran.

A resume reserves the attempt it is about to start, keyed on the run's receipts,
and the launch then adopts that reservation. The launch looked for it only from
one past the attempt the pointer names. An attempt that died before leaving a
receipt has not consumed its number, so the door reserved that same attempt,
the launch skipped its reservation, reserved the attempt again, met its own
live file and refused itself: a run whose wait-lift died before its worker
started could not be resumed at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch

RUN = "r-20261115T000000000000-never-started-attempt"


def _run_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two attempts with receipts, then a third recorded and never started."""
    directory = tmp_path / "runs" / RUN
    directory.mkdir(parents=True)
    for attempt in (1, 2):
        (directory / f"attempt-{attempt}-exit.json").write_text(
            json.dumps({"attempt": attempt, "exit_code": 0}), encoding="utf-8"
        )
    (directory / "attempt.json").write_text(
        json.dumps({"attempt": 3, "attempt_kind": "resume", "run_id": RUN}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatch_launch, "_pointer_attempt_number", lambda d: 3)
    monkeypatch.setattr(
        "reckon.crew.dispatch_refusals.refuse_a_released_launch", lambda run_id: None
    )
    return directory


def test_the_floor_is_the_receipt_key_when_the_pointer_runs_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _run_directory(tmp_path, monkeypatch)
    # The pointer names attempt 3, so one past it is 4; no receipt holds 3.
    assert dispatch_launch._adoptable_floor(directory, 4) == 3


def test_a_launch_adopts_the_never_started_attempt_its_door_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _run_directory(tmp_path, monkeypatch)
    reservation = dispatch_launch.reserve_launch_attempt(
        directory, RUN, kind="resume"
    )
    assert reservation.attempt == 3

    adopted = dispatch_launch._reservation_taken_by_this_process(
        directory, at_least=dispatch_launch._adoptable_floor(directory, 4)
    )

    assert adopted is not None
    assert adopted.attempt == 3
    assert adopted.path == reservation.path


def test_the_floor_stays_one_past_the_pointer_when_receipts_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal resume after a finished attempt is unchanged."""
    directory = _run_directory(tmp_path, monkeypatch)
    (directory / "attempt-3-exit.json").write_text(
        json.dumps({"attempt": 3, "exit_code": 0}), encoding="utf-8"
    )
    assert dispatch_launch._adoptable_floor(directory, 4) == 4
