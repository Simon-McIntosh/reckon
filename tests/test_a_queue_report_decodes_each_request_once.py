"""One queue report decodes each promotion request at most once.

The queue report lists the whole request directory on every station tick that
has a queued promotion, and at the base revision it decoded every file there
each time. The station's 1,600 requests held 16% of a tick's samples, JSON
decoding 21.7% of that self time. The tests below build a directory large
enough to measure, count decodes through a stand-in for the request reader, and
hold each report to the report the base revision produced for the same files —
first over an untouched directory, then over one request rewritten into a new
phase.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import promotion_requests as requests
from reckon.crew import runs
from reckon.crew.node import CrewError
from reckon.crew.queue_order import _override, fair_queue_order_details

PROJECTS = ("alpha-project", "beta-project", "gamma-project")
# Two sessions per project, so the fixture spans six owners of one directory.
SESSIONS = tuple(
    (project, f"{project}-session-{index}") for project in PROJECTS for index in (1, 2)
)
REQUEST_COUNT = 400
# Every phase the report must read the directory through: the queued phases it
# reports, and the terminal phases it decodes but raises nothing for.
PHASE_MIX = (
    (requests.REQUESTED, 100),
    (requests.RUNNING, 100),
    (requests.SETTLED, 150),
    (requests.FAILED, 50),
)
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
WINDOW_SECONDS = 3600.0


class Fixture:
    """The written paths and the request each of them holds."""

    def __init__(self, paths: list[Path], records: dict[str, dict[str, Any]]) -> None:
        self.paths = paths
        self.records = records

    def path_of(self, run_id: str) -> Path:
        return next(path for path in self.paths if path.stem == run_id)


def _record(
    run_id: str, project: str, session: str, phase: str, index: int
) -> dict[str, Any]:
    record = {
        "run_id": run_id,
        "project": project,
        "session": session,
        "node": f"node-of-{run_id}",
        "plan": f"reckon:{project}-plan",
        "phase": phase,
        "request_id": f"id-of-{run_id}",
        # A spread of ages either side of the starvation threshold, so both the
        # ageing class and the session fair-share class are ordered.
        "requested_at": (NOW - timedelta(minutes=5 + index % 240)).isoformat(),
        "options": {"config": None, "gate": "passed"},
    }
    # Some requests carry the priority snapshot a dispatch writes; the rest
    # inherit it from a pointer that is not there, which reads as none.
    if index % 7 == 0:
        record["priority_override"] = index % 3 + 1
    return record


def _build(directory: Path) -> Fixture:
    """Write the 400 requests, spread over every phase and every owner."""
    paths: list[Path] = []
    records: dict[str, dict[str, Any]] = {}
    index = 0
    for phase, count in PHASE_MIX:
        for _ in range(count):
            project, session = SESSIONS[index % len(SESSIONS)]
            label = phase.removeprefix("promotion-")
            run_id = f"r-20261120T000000{index:03d}-{label}"
            record = _record(run_id, project, session, phase, index)
            path = directory / f"{run_id}.json"
            runs._write_json(path, record)
            paths.append(path)
            records[run_id] = record
            index += 1
    assert len(paths) == REQUEST_COUNT
    return Fixture(paths, records)


def _base_pending_requests(paths: list[Path], moment: datetime) -> list[dict[str, Any]]:
    """The loop ``_pending_requests`` ran at the base revision.

    A transcription: list the directory, decode every file uncached, keep the
    requests whose phase is queued, inherit a missing priority snapshot from the
    run's own pointer, then order by the same fairness pass. Decoding every file
    on every call is exactly the behaviour this section removes, so this
    yardstick stays a plain ``json.loads`` rather than reading through the memo.
    """
    pending = []
    for path in paths:
        request = json.loads(path.read_text())
        if request.get("phase") in requests.QUEUED:
            if "priority_override" not in request:
                with suppress(CrewError):
                    request["priority_override"] = _override(
                        runs.read_pointer(str(request["run_id"]))
                    )
            pending.append(request)
    waiting = {str(request.get("run_id") or "") for request in pending}
    review_first = {
        str(request.get("run_id") or "")
        for request, reviewed in requests._reviewed_request_runs(pending)
        if reviewed in waiting
    }
    served = Counter(
        str(row["session"])
        for row in requests._recent_drained(moment, WINDOW_SECONDS)
        if row.get("phase") == requests.PROMOTED and row.get("session")
    )
    served.update(
        str(request.get("session") or "")
        for request in pending
        if request.get("phase") == requests.RUNNING
    )
    return [
        {**request, "ordering_reason": reason}
        for request, reason in fair_queue_order_details(
            pending,
            now=moment,
            joined_at=lambda request: requests.parse_utc(request.get("requested_at")),
            is_review=lambda request: str(request.get("run_id") or "") in review_first,
            served=served,
            consumes_turn=lambda request: request.get("phase") == requests.REQUESTED,
        )
    ]


def _base_report(paths: list[Path], moment: datetime) -> dict[str, Any]:
    """The queue report the base revision produced for these files."""
    waiting: list[dict[str, Any]] = []
    running = 0
    parked = 0
    for request in _base_pending_requests(paths, moment):
        phase = request.get("phase")
        if phase == requests.REQUESTED:
            waiting.append(request)
        elif phase == requests.RUNNING:
            running += 1
        elif phase == requests.WAITING:
            parked += 1
    oldest = None
    for request in waiting:
        requested_at = requests.parse_utc(str(request.get("requested_at")))
        if requested_at is None:
            continue
        if oldest is None or requested_at < oldest[0]:
            oldest = (requested_at, request)
    counted = {"promoted": 0, "refused": 0, "failed": 0, "deferred": 0}
    stages: Counter[str] = Counter()
    for row in requests._recent_drained(moment, WINDOW_SECONDS):
        for stage, elapsed in (row.get("stage_seconds") or {}).items():
            stages[str(stage)] += float(elapsed)
        key = {
            requests.PROMOTED: "promoted",
            requests.REFUSED: "refused",
            requests.FAILED: "failed",
            requests.DEFERRED: "deferred",
        }.get(str(row.get("phase")))
        if key is not None:
            counted[key] += 1
    return {
        "waiting": len(waiting),
        "running": running,
        "parked": parked,
        "requests": [
            {
                "run_id": request["run_id"],
                "session": request.get("session", ""),
                "project": str(request.get("project") or ""),
                "requested_at": str(request.get("requested_at") or ""),
                "position": position,
                "reason": request["ordering_reason"],
            }
            for position, request in enumerate(waiting, 1)
        ],
        "oldest_wait_seconds": (
            None
            if oldest is None
            else round(max(0.0, (moment - oldest[0]).total_seconds()), 3)
        ),
        "oldest_wait_run_id": None if oldest is None else oldest[1].get("run_id"),
        "window_seconds": WINDOW_SECONDS,
        "finished_last_window": sum(counted.values()),
        "promoted_last_window": counted["promoted"],
        "refused_last_window": counted["refused"],
        "failed_last_window": counted["failed"],
        "deferred_last_window": counted["deferred"],
        "successful_promotions_per_hour": (
            counted["promoted"] * 3600 / WINDOW_SECONDS if WINDOW_SECONDS > 0 else None
        ),
        "stage_seconds_last_window": dict(stages),
        "rate_bounded_by": max(stages, key=stages.get) if stages else None,
    }


@pytest.fixture
def directory(home: Path) -> Path:
    """A config home holding only this test's promotion requests."""
    target = requests._requests_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def decodes(monkeypatch) -> Counter[str]:
    """Count every decode through a stand-in for the request reader."""
    counts: Counter[str] = Counter()
    read = requests._read

    def counting(path):
        counts[str(path)] += 1
        return read(path)

    monkeypatch.setattr(requests, "_read", counting)
    return counts


def test_a_report_decodes_each_request_once_and_an_unchanged_report_none(
    directory: Path, decodes: Counter[str]
):
    """One call decodes the population once; an unchanged second call decodes none.

    Every request is held to the report the base revision produced for the same
    files, so the saving is measured against an unchanged answer, not against a
    faster one.
    """
    fixture = _build(directory)
    expected = _base_report(fixture.paths, NOW)

    first = requests.promotion_queue_report(now=NOW, window_seconds=WINDOW_SECONDS)
    first_decodes = dict(decodes)
    print(
        f"first report over {len(fixture.paths)} requests decoded "
        f"{sum(first_decodes.values())} times; most any request was decoded: "
        f"{max(first_decodes.values(), default=0)}"
    )
    # The instrument saw the whole population, so the zeros below are an absence
    # of repeat decodes rather than an absence of a reader.
    assert len(first_decodes) == len(fixture.paths)
    assert set(first_decodes.values()) == {1}
    assert first == expected

    decodes.clear()
    second = requests.promotion_queue_report(now=NOW, window_seconds=WINDOW_SECONDS)
    print(f"second report over the unchanged directory decoded {sum(decodes.values())}")
    assert dict(decodes) == {}
    assert second == expected


def test_a_rewritten_request_is_decoded_again_and_reported_in_its_new_phase(
    directory: Path, decodes: Counter[str]
):
    """Only a request whose file moved is decoded, and it reads in its new phase."""
    fixture = _build(directory)
    first = requests.promotion_queue_report(now=NOW, window_seconds=WINDOW_SECONDS)
    assert first == _base_report(fixture.paths, NOW)

    settled = next(
        run_id
        for run_id, record in fixture.records.items()
        if record["phase"] == requests.SETTLED
    )
    assert settled not in {row["run_id"] for row in first["requests"]}
    path = fixture.path_of(settled)
    rewritten = json.loads(path.read_text())
    rewritten["phase"] = requests.REQUESTED
    rewritten["requested_at"] = (NOW - timedelta(hours=2)).isoformat()
    runs._write_json(path, rewritten)

    decodes.clear()
    third = requests.promotion_queue_report(now=NOW, window_seconds=WINDOW_SECONDS)
    print(
        f"third report after one rewrite decoded {sorted(Path(p).stem for p in decodes)}"
    )
    assert {Path(path).stem for path in decodes} == {settled}
    assert sum(decodes.values()) == 1

    entries = {row["run_id"]: row for row in third["requests"]}
    assert settled in entries
    assert entries[settled]["requested_at"] == rewritten["requested_at"]
    assert third["waiting"] == first["waiting"] + 1
    assert third == _base_report(fixture.paths, NOW)
