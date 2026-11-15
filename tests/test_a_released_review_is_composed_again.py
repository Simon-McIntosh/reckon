"""A composed review the admission pass released is composed again.

A composed review is written as a queued request when the lane holds it, the
admission pass launches it, and a launch that stops before it spawns is
released by that pass: the claim, the pointer and the whole run directory go.
The run the review stood for still owes one, and the next reflex pass composes
it again for the same run and head.

The base defect this pins: the release removed the review and wrote nothing
back to the reviewed run, whose dispatch record still named the released
attempt as ``dispatched`` on the lane at that head. The next reflex pass read
that record as "the lane already dropped this revision" and composed nothing —
``no eligible lane (backend 'clive' already dropped it)`` — so the review the
admission pass had accepted was lost until a coordinator dispatched it by hand.
The release now records the outcome on the reviewed run's own record, the same
status the reflex writes when it releases a stranded standing review itself.

A reviewed run the fleet has since promoted or discarded owes no review, so a
release found beside one stamps nothing and composes nothing.

The follower and watch-seat machinery is incidental here, so both are stubbed
with the shape a dispatch needs to proceed, as the session-delivery tests do.
"""

# ruff: noqa: F811 - the fixture is imported and also named as a test parameter.

from __future__ import annotations

import copy
import importlib
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import (
    promotion,
    queue_admission,
    recovery_review_dispatch,
    runs,
)
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixtures
    CONFIG,
    PROJECT,
    SESSION,
    _lane_document,
    admit_repo,
)
from tests.test_a_review_is_not_refused_for_its_run import _delivered_scoring_run

REVIEWED_RUN = "r-delivered-awaiting-review"
RELEASE_BOUND = 900


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _config(tmp_path: Path, *, worker_slots: float) -> dict:
    config = copy.deepcopy(CONFIG)
    config.setdefault("fences", {})["unreconciled_run_grace"] = "1s"
    config["roles"]["review"] = {}
    config["backends"]["clive"]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=worker_slots)
    )
    return config


def _resolve(config: dict):
    def resolve(pointer):
        return config

    return resolve


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _watching(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let dispatches past the watch seat and the session's follower.

    Neither carries this measure: a delivery refusal in front of the launch
    would replace the release under test with the follower's own refusal.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    watch_state = {
        "watcher_live": True,
        "session_attached": True,
        "session_follower_released": False,
        "attach_line": "reckon crew follow --project proj",
        "ensure_line": "reckon crew watch --project proj",
    }
    monkeypatch.setenv("RECKON_WATCH_ARMING", "on")
    monkeypatch.setattr(
        dispatch_module, "_ensure_watch_producer", lambda *a, **k: dict(watch_state)
    )
    monkeypatch.setattr(
        dispatch_module, "_watcher_delivery_admission", lambda *a, **k: None
    )


def _delivered_run(repo: Path) -> dict:
    """The run under review, recorded as delivered rather than still starting."""
    record = _delivered_scoring_run(repo, run_id=REVIEWED_RUN)
    record["phase"] = "complete"
    runs._write_json(runs.pointer_path(REVIEWED_RUN), record)
    return record


def _head(record: dict) -> str:
    fields = recovery_review_dispatch._review_dispatch_fields(record)
    return str(fields["head"])


def _composed_review(repo: Path, tmp_path: Path) -> tuple[dict, str]:
    """Dispatch the composed review an open lane admits."""
    record = _delivered_run(repo)
    config = _config(tmp_path, worker_slots=1)
    report = recovery_review_dispatch.dispatch_review_for_run(
        record, config=config, launcher=_launcher([])
    )
    review_run_id = str(report.get("review_run_id") or "")
    assert report.get("dispatched") is True, report
    assert review_run_id
    recorded = runs.read_pointer(REVIEWED_RUN)[
        recovery_review_dispatch.REVIEW_DISPATCH_FIELD
    ]
    assert recorded["status"] == "dispatched"
    assert recorded["run_id"] == review_run_id
    return record, review_run_id


def _strand(review_run_id: str) -> float:
    """Leave the review as a launch that stopped before it spawned a worker.

    The pointer holds a pre-spawn phase and no pid; the run directory holds no
    worker record, no attempt record and no stream; and the claim names this
    process as holder, which began after the claim was published, so this host
    cannot prove the claim is still held. That is the shape the admission
    pass's own release reads, and it is manufactured by removing the evidence a
    launch would have written rather than by editing a verdict: the reader under
    test decides, not the fixture.
    """
    launched = time.time() - RELEASE_BOUND - 60
    pointer = runs.read_pointer(review_run_id)
    directory = runs.run_dir(review_run_id)
    for name in ("worker.json", "claim.json", "attempt.json"):
        (directory / name).unlink(missing_ok=True)
    pointer.pop("pid", None)
    pointer.pop("stderr_path", None)
    pointer["phase"] = "starting"
    pointer["created_at"] = _at(launched)
    pointer["attempt_started_at"] = _at(launched)
    runs._write_json(runs.pointer_path(review_run_id), pointer)
    runs._write_json(
        directory / "claim.json",
        {
            "run_id": review_run_id,
            "claim_registered_at": _at(launched),
            "holder_pid": os.getpid(),
            "holder_host": socket.gethostname(),
            "launched_at": _at(launched),
            "step": "claim-published",
            "step_at": _at(launched),
        },
    )
    return time.time()


def _release() -> list[str]:
    return queue_admission._release_stranded_launches(
        runs.list_live(project=PROJECT), now_seconds=time.time()
    )


def test_a_released_composed_review_is_composed_again(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _watching(monkeypatch)
    record, review_run_id = _composed_review(admit_repo, tmp_path)
    head = _head(record)
    assert head

    _strand(review_run_id)
    assert _release() == [review_run_id]
    assert not runs.pointer_path(review_run_id).exists()
    assert not runs.run_dir(review_run_id).exists()

    recorded = runs.read_pointer(REVIEWED_RUN)[
        recovery_review_dispatch.REVIEW_DISPATCH_FIELD
    ]
    assert recorded["status"] == "released-stranded-launch", recorded
    assert review_run_id in recorded["reason"]

    # The run still stands at the head the release was for.
    refreshed = runs.read_pointer(REVIEWED_RUN)
    assert refreshed["run_id"] == REVIEWED_RUN
    again = recovery_review_dispatch.dispatch_review_for_run(
        refreshed, config=_config(tmp_path, worker_slots=1), launcher=_launcher([])
    )

    assert again.get("dispatched") is True, again
    composed = str(again.get("review_run_id") or "")
    assert composed and composed != review_run_id
    assert runs.pointer_path(composed).is_file()
    assert again["dispatched"] is True
    # The composition is for the same reviewed run and head the release was for.
    assert _head(runs.read_pointer(REVIEWED_RUN)) == head
    assert (
        recovery_review_dispatch._review_in_flight(runs.read_pointer(REVIEWED_RUN))
        == composed
    )


def test_a_released_review_of_a_gone_run_is_not_composed_again(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _watching(monkeypatch)
    record, review_run_id = _composed_review(admit_repo, tmp_path)

    # The fleet ends the reviewed run while its review is still stranded.
    promotion.discard(REVIEWED_RUN)
    assert promotion.discard_recorded(REVIEWED_RUN)
    assert not runs.pointer_path(REVIEWED_RUN).exists()

    _strand(review_run_id)
    assert _release() == [review_run_id]
    assert not runs.pointer_path(review_run_id).exists()
    assert not runs.run_dir(review_run_id).exists()
    # Nothing written back: the run departed, so no record of it is resurrected
    # for the next pass to read.
    assert not runs.pointer_path(REVIEWED_RUN).exists()

    # The next pass finds nothing to compose: no review stands for the run and
    # no live pointer carries it, so the release does not read as an obligation.
    assert recovery_review_dispatch._review_in_flight(record) == ""
    assert [
        pointer
        for pointer in runs.list_live(project=PROJECT)
        if REVIEWED_RUN in str((pointer.get("node") or {}).get("id") or "")
    ] == []
