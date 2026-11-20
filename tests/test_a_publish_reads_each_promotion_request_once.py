"""One obligations publish decodes each promotion request at most once.

The request directory holds one file per promotion attempt, and every session's
slice of a publish filters it by project and session. Reading the directory per
slice meant one publish decoded every file once per subscribed session, so the
station's 1,578 requests over six sessions decoded 28 MB for each of them, 11.9%
of every sample in the tick's profile. The tests below build a directory large
enough to measure, count the decodes through a stand-in for the request reader,
and hold the duties and the settlements to the reading the base revision
produced for the same files.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon._timestamps import parse_utc
from reckon.crew import promotion_requests as requests
from reckon.crew import runs

PROJECTS = ("alpha-project", "beta-project", "gamma-project")
# Two sessions per project, so the six sessions of one publish read a directory
# holding requests for all three.
SESSIONS = tuple(
    (project, f"{project}-session-{index}") for project in PROJECTS for index in (1, 2)
)
REQUEST_COUNT = 400
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
REQUESTED_AT = (NOW - timedelta(hours=3)).isoformat()
FINISHED_AT = (NOW - timedelta(hours=2)).isoformat()
CHECKED_AT = (NOW - timedelta(hours=1)).isoformat()


def _record(run_id: str, project: str, session: str, phase: str, **extra) -> dict:
    record = {
        "run_id": run_id,
        "project": project,
        "session": session,
        "node": f"node-of-{run_id}",
        "plan": f"reckon:{project}-plan",
        "phase": phase,
        "request_id": f"id-of-{run_id}",
        "requested_at": REQUESTED_AT,
        "options": {"config": None, "gate": "passed"},
    }
    record.update(extra)
    return record


class Fixture:
    """The written paths, and what the directory held settled before any read."""

    def __init__(self, paths: list[Path], settled_before: set[str]) -> None:
        self.paths = paths
        self.settled_before = settled_before

    def path_of(self, run_id: str) -> Path:
        return next(path for path in self.paths if path.stem == run_id)


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    """A config home holding only this test's crew state."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    directory = requests._requests_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _build(directory: Path) -> Fixture:
    """Write the 400 requests, every failed or waiting run carrying a pointer.

    Each of the six sessions owns two failures and one overdue wait, which the
    publish raises as duties because each run still has a live pointer. One
    failure in the whole directory has no pointer, so its run can no longer be
    promoted and the publish settles it and rewrites its file. The rest of the
    population is already settled, so no reader decodes it into a duty.
    """
    paths: list[Path] = []
    settled_before: set[str] = set()
    counter = 0

    def run_id(label: str) -> str:
        nonlocal counter
        counter += 1
        return f"r-20261120T000000{counter:03d}-{label}"

    def write(identifier: str, project: str, session: str, phase: str, **extra):
        path = directory / f"{identifier}.json"
        runs._write_json(path, _record(identifier, project, session, phase, **extra))
        paths.append(path)
        if phase == requests.SETTLED:
            settled_before.add(identifier)
        return path

    def alive(label: str, project: str, session: str, phase: str, **extra) -> None:
        identifier = run_id(label)
        runs._write_json(
            runs.pointer_path(identifier),
            {"run_id": identifier, "project": project, "session": session},
        )
        write(identifier, project, session, phase, **extra)

    for project, session in SESSIONS:
        for attempt in (1, 2):
            alive(
                f"failed-{attempt}",
                project,
                session,
                requests.FAILED,
                finished_at=FINISHED_AT,
                error=f"the manifest reads incomplete (attempt {attempt})",
            )
        alive(
            "waiting",
            project,
            session,
            requests.WAITING,
            checked_at=CHECKED_AT,
            waiting_reason="the review run's manifest does not read complete",
        )

    gone = run_id("no-pointer")
    write(
        gone,
        PROJECTS[0],
        SESSIONS[0][1],
        requests.FAILED,
        finished_at=FINISHED_AT,
        error="the landing checkout refused the head",
    )

    while len(paths) < REQUEST_COUNT:
        identifier = run_id("settled")
        project, session = SESSIONS[len(paths) % len(SESSIONS)]
        write(identifier, project, session, requests.SETTLED)
    assert len(settled_before) == REQUEST_COUNT - len(SESSIONS) * 3 - 1
    return Fixture(paths, settled_before)


def _base_projection(
    paths: list[Path], moment: datetime
) -> tuple[dict[tuple[str, str], list[tuple]], set[str]]:
    """The duties and settlements the base revision produced for these files.

    A transcription of the loop ``failure_obligations`` ran at the base
    revision: list the directory, decode every file, skip a phase no duty is
    raised for, keep the caller's project and session, and settle a request
    whose run has arrived where the refusal pointed or has gone altogether. The
    reader's own predicate decides the settlement, and the ages are the base
    revision's own arithmetic.
    """
    duties: dict[tuple[str, str], list[tuple]] = {}
    settled: set[str] = set()
    for project, session in SESSIONS:
        rows: list[tuple] = []
        for path in paths:
            request = json.loads(path.read_text())
            waiting = request.get("phase") == requests.WAITING
            if request.get("phase") not in requests.ERRORS and not waiting:
                continue
            if request.get("project") != project or request.get("session") != session:
                continue
            if requests._settled_state(request):
                settled.add(request["run_id"])
                continue
            if waiting:
                age = requests._waited_seconds(
                    request.get("requested_at"), moment.isoformat()
                )
                if age is None or age <= requests.WAITING_OBLIGATION_SECONDS:
                    continue
                rows.append(
                    (
                        requests.WAITING,
                        request["run_id"],
                        int(age),
                        request.get("checked_at") or "unknown",
                    )
                )
                continue
            finished = parse_utc(request.get("finished_at")) or moment
            rows.append(
                (
                    "promotion-failed",
                    request["run_id"],
                    max(0, int((moment - finished).total_seconds())),
                    str(request.get("error") or "promotion refused"),
                )
            )
        duties[(project, session)] = sorted(rows)
    return duties, settled


def _rows(duties: dict[tuple[str, str], list[dict]]) -> dict[tuple[str, str], list]:
    """The measured fields of each session's duty rows, in a comparable order."""
    measured: dict[tuple[str, str], list[tuple]] = {}
    for key, items in duties.items():
        rows = []
        for item in items:
            if item["kind"] == requests.WAITING:
                rows.append(
                    (
                        requests.WAITING,
                        item["run_id"],
                        item["age_seconds"],
                        item["checked_at"],
                    )
                )
            else:
                rows.append(
                    (
                        "promotion-failed",
                        item["run_id"],
                        item["age_seconds"],
                        item["reason"],
                    )
                )
        measured[key] = sorted(rows)
    return measured


def _settled_now(paths: list[Path], before: set[str]) -> set[str]:
    """The runs this read settled, being those its rewrite made settled."""
    settled: set[str] = set()
    for path in paths:
        record = json.loads(path.read_text())
        if path.stem in before:
            assert record.get("phase") == requests.SETTLED, path
            continue
        if record.get("phase") == requests.SETTLED:
            assert record.get("settled"), path
            settled.add(record["run_id"])
    return settled


@pytest.fixture
def decodes(monkeypatch):
    """Count every decode through a stand-in for the request reader."""
    counts: Counter[str] = Counter()
    read = requests._read

    def counting(path):
        counts[str(path)] += 1
        return read(path)

    monkeypatch.setattr(requests, "_read", counting)
    return counts


def test_each_session_reads_the_directory_once(crew_home, decodes):
    """A publish's six session reads decode each request once, not six times.

    This is the call shape the publish used at the base revision — one
    ``failure_obligations`` per subscribed session, nothing shared between them
    but the process. At the base revision every one of those calls decoded every
    file: 2,400 decodes for 400 requests over six sessions.
    """
    fixture = _build(crew_home)
    expected_duties, expected_settled = _base_projection(fixture.paths, NOW)

    first = {
        key: requests.failure_obligations(key[0], key[1], now=NOW) for key in SESSIONS
    }
    counts = dict(decodes)
    print(
        f"one publish over {len(SESSIONS)} sessions decoded "
        f"{sum(counts.values())} times for {len(fixture.paths)} requests; "
        f"most any request was decoded: {max(counts.values(), default=0)}"
    )

    # The instrument saw the whole population, so the counts below are an
    # absence of repeat decodes rather than an absence of decodes.
    assert len(counts) == len(fixture.paths)
    repeated = {path for path, count in counts.items() if count > 1}
    assert repeated == {str(fixture.path_of(expected_settled.copy().pop()))}
    assert sum(counts.values()) == len(fixture.paths) + 1

    assert _rows(first) == expected_duties
    assert _settled_now(fixture.paths, fixture.settled_before) == expected_settled


def test_a_publish_shares_one_reading_across_its_sessions(crew_home, decodes):
    """One index serves every session, and a publish rewrote at most one file."""
    fixture = _build(crew_home)
    expected_duties, expected_settled = _base_projection(fixture.paths, NOW)

    index = requests.request_index()
    sliced = {
        key: requests.failure_obligations(key[0], key[1], now=NOW, index=index)
        for key in SESSIONS
    }
    counts = dict(decodes)
    print(
        f"one index over {len(fixture.paths)} requests decoded "
        f"{sum(counts.values())} times; most any request was decoded: "
        f"{max(counts.values(), default=0)}"
    )
    assert len(counts) == len(fixture.paths)
    assert set(counts.values()) == {1}
    assert _rows(sliced) == expected_duties
    assert _settled_now(fixture.paths, fixture.settled_before) == expected_settled

    # A second publish decodes only what the first rewrote: every other request
    # is still the version the first publish read.
    decodes.clear()
    second = {
        key: requests.failure_obligations(key[0], key[1], now=NOW) for key in SESSIONS
    }
    rewritten = {Path(path).stem for path in decodes}
    print(f"second publish decoded {sorted(rewritten)}")
    assert rewritten == expected_settled
    assert set(decodes.values()) == {1}
    assert _rows(second) == expected_duties
