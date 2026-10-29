"""The queue reports its own stalls where it is read.

An admission pass recorded only what it did: a request it launched, held or
refused. A request that stayed admissible on a lane with room while the pass was
busy elsewhere appeared in no row at all, so a queue that had waited eleven
minutes and a lane that had never had anything to do read the same, and a lead
reading an idle lane twice mistook a halted system for slack.

A pass now records one stall verdict for a request that has waited past ten
minutes on a lane with room and that nothing holds: the request, the stage it is
owed — release, launch or land — and that stage's duration on the last pass, so
the record names its own cause. The verdict is written once and stands until a
later verdict for the same request replaces it, so a view reading the last
verdict tells a request that was stalled from one that has since launched. The
drain view reads that record instead of resolving a request's waits a second
time to decide what is stuck, and the age of the oldest request the lane could
launch is carried on the pass and on every row it writes.

A request the lane has no room for, and one a live run's claim holds, record no
stall: their wait is accounted for by the hold the pass already names.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew import needs_you, queue_admission, runs
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
chain_repo = fixtures.chain_repo
RUN_ID = "r-n1"
#: Eleven minutes: past the ten-minute threshold by one, which is the wait the
#: lane reported without a record on 28 October.
WAITED_SECONDS = 11 * 60
#: The launch stage's duration on the pass before this one.
LAST_LAUNCH_SECONDS = 12.5


def _log_path():
    return runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG


def _write_log(records: list[dict]) -> None:
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    _log_path().write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _append_log(record: dict) -> None:
    with _log_path().open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def _queue_one(repository, *, waited_seconds: float = WAITED_SECONDS) -> dict:
    """One request on a ready section, queued as of ``waited_seconds`` ago."""
    stored = queue_admission.queue_requests(
        [fixtures._node("n1", "s1")],
        project=PROJECT,
        session=fixtures.SESSION,
        repo_root=repository,
        config=fixtures.CONFIG,
        run_ids={"n1": RUN_ID},
    )
    assert stored[RUN_ID]["phase"] == queue_admission.QUEUED_PHASE, stored[RUN_ID]
    pointer = dict(runs.read_pointer(RUN_ID))
    pointer["queued_at"] = (
        datetime.now(UTC) - timedelta(seconds=waited_seconds)
    ).isoformat()
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    return pointer


def _admit(*, launcher=None, config_for=None) -> dict:
    recorded: list[str] = []
    return queue_admission.admit_once(
        config_for=config_for or fixtures._config_for(),
        launcher=launcher or fixtures._launcher(recorded),
    )


def _stalls(result: dict) -> list[dict]:
    return [
        row
        for row in result.get("verdicts") or ()
        if row.get("verdict") == queue_admission.STALL_VERDICT
    ]


def _previous_pass(**durations) -> dict:
    """A verdict-log line as an earlier pass wrote it."""
    record = {
        "at": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
        "pointers": [],
    }
    record.update(durations)
    return record


def test_an_eleven_minute_wait_on_a_lane_with_room_records_one_stall(
    chain_repo,
) -> None:
    """The wait the lane could have taken is recorded with the stage it waited on."""
    _write_log(
        [_previous_pass(release_seconds=1.0, launch_seconds=LAST_LAUNCH_SECONDS)]
    )
    _queue_one(chain_repo)
    stream = runs.watch_stream_path(PROJECT)
    assert not stream.exists()

    result = _admit()

    stalls = _stalls(result)
    assert len(stalls) == 1, result["verdicts"]
    row = stalls[0]
    assert row["run_id"] == RUN_ID
    assert row["stage"] in {"release", "launch", "land"}, row
    assert row["stage_seconds"] == LAST_LAUNCH_SECONDS, row
    assert row["age_seconds"] >= WAITED_SECONDS, row
    # The age of the oldest request the lane could launch is on the pass and on
    # every row it wrote, so a row that names a hold also says how old the queue
    # around it was.
    assert result["admissible_age_seconds"] is not None
    assert result["admissible_age_seconds"] >= WAITED_SECONDS
    for verdict in result["verdicts"]:
        assert verdict["admissible_age_seconds"] == result["admissible_age_seconds"]
    # The stall is the queue's record: no monitor-stream row is written for it.
    assert not stream.exists()


def test_a_stall_is_recorded_once_and_clears_on_a_later_verdict(chain_repo) -> None:
    """A wait spanning many passes yields one stall, and a launch clears it."""
    _write_log([_previous_pass(launch_seconds=LAST_LAUNCH_SECONDS)])
    _queue_one(chain_repo)
    first = _stalls(_admit())
    assert len(first) == 1, first
    # The service writes the pass's record; replay that as the pass after it.
    record = {"at": datetime.now(UTC).isoformat(), "pointers": [first[0]]}
    _write_log([_previous_pass(launch_seconds=LAST_LAUNCH_SECONDS), record])
    runs._write_json(runs.pointer_path(RUN_ID), _queue_one(chain_repo))

    second = _stalls(_admit())

    assert second == [], second


def test_a_recorded_stall_reaches_its_session_once_until_it_clears(
    chain_repo,
) -> None:
    """The request's own session sees one row for the stall, and a launch clears it."""
    _write_log([_previous_pass(launch_seconds=LAST_LAUNCH_SECONDS)])
    pointer = _queue_one(chain_repo)
    recorded = _stalls(_admit())
    assert len(recorded) == 1, recorded
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    _write_log([{"at": datetime.now(UTC).isoformat(), "pointers": [recorded[0]]}])

    rows, _counts = needs_you.needs_you_rows(PROJECT, session=fixtures.SESSION)

    stalled = [row for row in rows if row.get("stalled")]
    assert len(stalled) == 1, rows
    assert stalled[0]["run_id"] == RUN_ID
    assert stalled[0]["kind"] == "blocked"
    assert stalled[0]["stage"] == recorded[0]["stage"]
    assert stalled[0]["age_seconds"] >= WAITED_SECONDS

    _append_log(
        {
            "at": datetime.now(UTC).isoformat(),
            "pointers": [{"run_id": RUN_ID, "verdict": "launched"}],
        }
    )
    cleared, _counts = needs_you.needs_you_rows(PROJECT, session=fixtures.SESSION)

    assert [row for row in cleared if row.get("stalled")] == []


def test_a_lane_with_no_room_or_a_live_claim_records_no_stall(
    chain_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wait the lane accounts for is not a stall."""
    _write_log([_previous_pass(launch_seconds=LAST_LAUNCH_SECONDS)])
    _queue_one(chain_repo)

    original_allowance = queue_admission._session_allowance_read
    monkeypatch.setattr(
        queue_admission,
        "_session_allowance_read",
        lambda pointer, *, config_for: (0, "no room on this lane"),
    )
    assert _stalls(_admit()) == []

    monkeypatch.setattr(
        queue_admission,
        "_session_allowance_read",
        original_allowance,
    )
    monkeypatch.setattr(
        queue_admission,
        "_live_claim_hold",
        lambda *args, **kwargs: "held by r-other over a shared path",
    )
    assert _stalls(_admit()) == []


@pytest.mark.parametrize("hold", sorted(queue_admission.HOLD_VERDICTS))
def test_a_request_a_pass_holds_is_never_stalled(
    chain_repo, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    """A hold says why the request waits, so the wait is not a stall.

    Every hold word is the pass accounting for a wait it decided on: a full
    lane, a gate, a live claim, an unchanged plan, a retry back-off, or an
    earlier request ahead of this one. Reporting one as a stall would accuse
    the queue of leaving undone work it is deliberately not doing, and it would
    put the stall row ahead of the hold that explains the wait.
    """
    _write_log([_previous_pass(launch_seconds=LAST_LAUNCH_SECONDS)])
    _queue_one(chain_repo)
    rows = [
        {
            "run_id": RUN_ID,
            "verdict": hold,
            "detail": "recorded by the pass before this one",
        }
    ]

    monkeypatch.setattr(
        queue_admission, "_last_verdict_of", lambda run_id: rows[0]["verdict"]
    )
    result = _admit()

    assert _stalls(result) == [], result["verdicts"]


def test_a_request_the_pass_holds_on_this_pass_is_never_stalled(
    chain_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold this pass writes is the row the request is owed, not a stall.

    The stall is recorded after the pass has decided what to do with each
    request, so a request held here carries its hold as the first row a reader
    sees rather than a stall the hold would have to explain away.
    """
    _write_log([_previous_pass(launch_seconds=LAST_LAUNCH_SECONDS)])
    _queue_one(chain_repo)
    monkeypatch.setattr(
        queue_admission,
        "_until_changed_hold",
        lambda pointer, *, config_for: "nothing the request depends on has moved",
    )

    result = _admit()

    assert _stalls(result) == [], result["verdicts"]
    assert result["verdicts"][0]["verdict"] == "held-until-changed", result["verdicts"]
    assert result["held"] == [RUN_ID], result["held"]


def test_a_request_waiting_on_its_dependencies_is_never_stalled(chain_repo) -> None:
    """A request waiting for upstream work to release is not the queue stalling.

    The stall rule is checked where it runs, because a pass promotes a request
    whose dependencies have landed before it decides anything: an admissible
    request in a waiting phase has, by that point, already stopped waiting.
    """
    _queue_one(chain_repo)
    pointer = dict(runs.read_pointer(RUN_ID))
    pointer["queued_at"] = (
        datetime.now(UTC) - timedelta(seconds=WAITED_SECONDS)
    ).isoformat()
    for phase in sorted(queue_admission.NOT_ADMISSIBLE_PHASES):
        waiting = dict(pointer, phase=phase)
        result = {"verdicts": []}

        stalled = queue_admission._record_stalls(
            [waiting],
            moment=datetime.now(UTC),
            gated=False,
            live=[],
            config_for=fixtures._config_for(),
            claim_dispositions={},
            shared_files={},
            held=set(),
            result=result,
        )

        assert stalled == [], (phase, result["verdicts"])
        assert result["verdicts"] == [], phase


def _drained(repository, monkeypatch, pointers, verdict_records):
    """The drain view over one seeded fleet, with the ledger and plans stubbed."""
    for pointer in pointers:
        runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)
    _write_log(verdict_records)
    monkeypatch.setattr(runs, "_drain_plan_states", lambda _project: [])
    monkeypatch.setattr(runs, "_unloaded_sections", lambda *_args: {})
    monkeypatch.setattr(
        "reckon.ledger.load", lambda *_args, **_kwargs: ({"runs": []}, 0)
    )
    return runs.drain(PROJECT)["week"]


def _pointer(run_id: str, phase: str, *, age_seconds: float) -> dict:
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": fixtures.SESSION,
        "node": {"plan": fixtures.PLAN, "section": "s1"},
        "phase": phase,
        "created_at": stamp,
        "queued_at": stamp,
    }


def test_the_drain_view_reports_the_queue_age_and_the_stall_stage(
    chain_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain view carries the queue's age and names the stage a stall holds."""
    pointer = _pointer(RUN_ID, queue_admission.QUEUED_PHASE, age_seconds=2 * 3600)
    week = _drained(
        chain_repo,
        monkeypatch,
        [pointer],
        [
            {
                "at": datetime.now(UTC).isoformat(),
                "admissible_age_seconds": 720.0,
                "pointers": [
                    {
                        "run_id": RUN_ID,
                        "verdict": queue_admission.STALL_VERDICT,
                        "stage": "launch",
                        "stage_seconds": LAST_LAUNCH_SECONDS,
                        "age_seconds": 700.0,
                    }
                ],
            }
        ],
    )

    assert week["project"]["admissible_age_seconds"] == 720.0
    stuck = {row["run_id"]: row for row in week["project"]["stuck"]}
    assert RUN_ID in stuck, week["project"]["stuck"]
    row = stuck[RUN_ID]
    assert row["stalled"] is True
    assert row["last_verdict"] == queue_admission.STALL_VERDICT
    assert row["stage"] == "launch"
    assert row["stage_seconds"] == LAST_LAUNCH_SECONDS
    assert row["age_seconds"] >= 3600


def test_a_launched_run_is_not_stuck(
    chain_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run a pass launched is working, however old it is.

    The stuck rule reads a recorded verdict, so it must say which verdicts still
    leave a request waiting. A launch verdict says the opposite: the request
    became a run, and its age is the worker's running time rather than a wait
    the queue owes anyone.
    """
    working = _pointer("r-working", "launched", age_seconds=2 * 3600)
    queued_without_a_verdict = _pointer(
        "r-quiet", queue_admission.QUEUED_PHASE, age_seconds=2 * 3600
    )
    week = _drained(
        chain_repo,
        monkeypatch,
        [working, queued_without_a_verdict],
        [
            {
                "at": datetime.now(UTC).isoformat(),
                "pointers": [{"run_id": "r-working", "verdict": "launched"}],
            }
        ],
    )

    stuck = {row["run_id"] for row in week["project"]["stuck"]}
    assert "r-working" not in stuck, week["project"]["stuck"]
    assert "r-quiet" not in stuck, week["project"]["stuck"]
    assert week["project"]["phases"]["launched"] == 1
