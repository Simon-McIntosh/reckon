"""A limit reset reaches every session's pane once, through the watch stream.

A budget reset is workstation-wide: every session stopped at its usage limit
may continue. Nothing about it is visible on the watch stream a follower reads
unless a producer writes it, so ``lift_watch.sweep_resets`` folds the recent
reset events into each project's stream and the pane draws one row per event.

The reset family runs beside the lift family rather than through it. A reset
and a lift concern the same budget group and both reach the pane once per
session, but a reset is not a lift and the pane draws it that way: its stage
cell reads ``limit``, it carries an instruction rather than a transition, and it
neither reads nor writes the pane's memory of the lift groups, so a group
holding a lift still draws its own baseline and end rows as before.

These use the lift-broadcast fixtures — a temporary crew home, the real
``take_lift_claim``, and ``follower_row_path`` — and drive the real
``lift_watch.sweep_resets``, so a measurement counts the rows the pane prints
rather than a fixture's opinion of them. Every fixture asserts afterwards that
the real crew home is untouched.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import (
    budget_reset,
    lift_watch,
    model_catalogue,
    recovery_watch,
    runs,
    ticker,
)
from reckon.crew.recovery_watch import format_watch_transition
from reckon.crew_follow_commands import follower_row_path, lift_claim_dir

SESSION_A = "s-power"
SESSION_B = "s-other"
GROUP = "claude-sub"
PROJECTS = ("alpha", "beta")
NOW = datetime(2026, 10, 14, 18, 30, tzinfo=UTC)
WIDE = 200


def _listing(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(str(path) for path in directory.rglob("*"))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the crew home at a temp dir; prove the real home is untouched."""
    real_claims = lift_claim_dir("")
    before = _listing(real_claims)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    yield tmp_path
    assert _listing(real_claims) == before, "a test wrote the real crew home"


# ── the producer's sweep ────────────────────────────────────────────────────


def _announce(
    *,
    moment: datetime = NOW,
    group: str = GROUP,
    clock: str = "seven_day",
    session: str | None = None,
    reason: str = "the lead said the limit reset",
    by: str = "lead",
) -> dict:
    """One ``used`` reset event in the store the sweep reads."""
    return budget_reset.mark_used(
        group,
        by=by,
        moment=moment,
        reason=reason,
        clock=clock,
        session=session,
    )


def _rows(stream: Path) -> list[dict]:
    if not stream.exists():
        return []
    return [
        json.loads(line) for line in stream.read_text().splitlines() if line.strip()
    ]


def _sweep(
    project: str,
    stream: Path,
    *,
    seen: set[str] | None,
    now: datetime = NOW,
) -> set[str]:
    """One clock-block pass of the reset sweep, into a named stream."""
    return lift_watch.sweep_resets(project, seen=seen, now=now, stream_path=stream)


def _reset_rows(rows: Sequence[dict]) -> list[dict]:
    return [row for row in rows if row.get("to_state") == lift_watch.LIMIT_RESET]


def _follower(session: str | None):
    return follower_row_path(session=session)


def _feed(path, rows: Sequence[dict], *, start: float = 1000.0) -> list[dict]:
    out: list[dict] = []
    now = start
    for row in rows:
        out.extend(path.feed(row, now=now))
        now += 1.0
    return out


def _rendered(row: dict) -> tuple[str, str]:
    """A row as both followers render it: the formatter and the ticker grid."""
    return (
        format_watch_transition(row, ticker=ticker.Ticker(width=WIDE)),
        ticker.Ticker(width=WIDE).render(row),
    )


def _lift_row(group: str = GROUP, *, kind: str = "transition", ended=None) -> dict:
    """One lift row in the producer's own shape, built by ``lift_watch``."""
    lift = {
        "group": group,
        "form": "five_hour",
        "pace_multiple": 1.8,
        "scope": "global",
        "id": f"{group}-lift",
    }
    return lift_watch._lift_row(PROJECTS[0], lift, kind=kind, ended=ended)


# ── one reset, one row per project, one row per pane ────────────────────────


def test_a_pane_following_both_projects_draws_the_reset_once(tmp_path):
    """The defect: one workstation reset written once into every stream."""
    _announce()
    streams = {project: tmp_path / f"{project}.events" for project in PROJECTS}
    for project in PROJECTS:
        seen = _sweep(project, streams[project], seen=None)
        rows = _reset_rows(_rows(streams[project]))
        assert len(rows) == 1, f"{project} appended {len(rows)} reset rows"
        assert rows[0]["lift_id"], "the row carries the event id the claim keys on"
        assert rows[0]["session"] == "", "a global reset is scoped to no session"
        assert seen

    followers = {project: _follower(SESSION_A) for project in PROJECTS}
    printed: list[dict] = []
    for project in PROJECTS:
        printed.extend(_feed(followers[project], _reset_rows(_rows(streams[project]))))

    drawn = _reset_rows(printed)
    assert len(drawn) == 1, f"session A's pane drew the reset {len(drawn)} times"
    for rendered in _rendered(drawn[0]):
        assert ticker.LIMIT_STAGE in rendered, "the stage cell names the reset"
        assert "may continue" in rendered, (
            "the row carries the instruction, not a state word"
        )
        assert "orchestrator" not in rendered, (
            "a worker lane's wallet claims nothing about orchestrator sessions"
        )


def test_a_second_sessions_pane_draws_it_once_too(tmp_path):
    _announce()
    streams = {project: tmp_path / f"{project}.events" for project in PROJECTS}
    for project in PROJECTS:
        _sweep(project, streams[project], seen=None)

    followers = {project: _follower(SESSION_B) for project in PROJECTS}
    printed: list[dict] = []
    for project in PROJECTS:
        printed.extend(_feed(followers[project], _reset_rows(_rows(streams[project]))))

    assert len(_reset_rows(printed)) == 1, "session B's pane drew it once"


# ── the horizon bounds a fresh (re)arm ──────────────────────────────────────


def test_a_producer_armed_two_hours_later_appends_no_row(tmp_path):
    _announce(moment=NOW - timedelta(hours=2))
    stream = tmp_path / "alpha.events"
    _sweep(PROJECTS[0], stream, seen=None)
    assert _rows(stream) == [], "a restart must not wake the fleet with an old reset"


def test_a_producer_armed_twenty_minutes_later_appends_a_row_the_pane_keeps(
    tmp_path,
):
    """The horizon admits it; the pane's claim still stops a second drawing."""
    _announce(moment=NOW - timedelta(minutes=20))
    stream = tmp_path / "alpha.events"

    first = _sweep(PROJECTS[0], stream, seen=None)
    rows = _reset_rows(_rows(stream))
    assert len(rows) == 1

    pane = _follower(SESSION_A)
    printed = _feed(pane, rows)
    assert len(_reset_rows(printed)) == 1

    # A second producer pass that lost its memory re-appends the same event,
    # because the horizon admits it — and the pane, already holding the claim,
    # draws nothing for it.
    assert _sweep(PROJECTS[0], stream, seen=None) == first
    again = _reset_rows(_rows(stream))[1:]
    assert len(rows) == 1 and len(again) > 0, "the pass re-appended the same event"
    assert _reset_rows(_feed(pane, again)) == [], "the pane drew the reset twice"


# ── the scope decides which panes see it ────────────────────────────────────


def test_a_session_scoped_event_reaches_its_own_pane_only(tmp_path):
    _announce(session=SESSION_B)
    stream = tmp_path / "alpha.events"
    _sweep(PROJECTS[0], stream, seen=None)
    row = _reset_rows(_rows(stream))[0]
    assert row["session"] == SESSION_B, "the row names the session it is scoped to"

    theirs = _feed(_follower(SESSION_B), [row])
    mine = _feed(_follower(SESSION_A), [row])
    assert len(_reset_rows(theirs)) == 1
    assert _reset_rows(mine) == [], "another session's reset reached this pane"


# ── a stated instant passes with nothing moving ─────────────────────────────


def test_a_future_event_waits_for_its_instant_with_no_file_changing(tmp_path):
    stated = NOW + timedelta(minutes=30)
    _announce(moment=stated)
    stream = tmp_path / "alpha.events"
    store = budget_reset.state_path()

    seen = _sweep(PROJECTS[0], stream, seen=None)
    assert _rows(stream) == [], "an instant still ahead announces nothing"
    before_store = store.stat().st_mtime_ns
    before_stream = stream.exists()
    assert seen == set()

    # Nothing changed in between but the clock: the second pass is the first
    # clock-block pass after the instant passes.
    later = _sweep(PROJECTS[0], stream, seen=seen, now=stated + timedelta(minutes=1))
    assert store.stat().st_mtime_ns == before_store, "a file moved between passes"
    assert before_stream is False, "the first pass wrote a stream"
    rows = _reset_rows(_rows(stream))
    assert len(rows) == 1, "the event did not reach the stream once it was due"
    assert later


def test_the_sweep_announces_an_event_once(tmp_path):
    """The ids the memory holds are the whole guard against a repeat."""
    _announce()
    stream = tmp_path / "alpha.events"
    seen = _sweep(PROJECTS[0], stream, seen=None)
    assert len(_reset_rows(_rows(stream))) == 1
    _sweep(PROJECTS[0], stream, seen=seen)
    assert len(_reset_rows(_rows(stream))) == 1, "the sweep re-announced the event"


# ── a consumed event is announced the same way ──────────────────────────────


def test_a_consumed_event_is_announced_as_a_used_one_is(tmp_path):
    budget_reset.mark_available(GROUP, by="lead", moment=NOW)
    window_minutes = 300
    seeded = NOW + timedelta(minutes=window_minutes)
    budget_reset.observe(
        GROUP,
        resets_at=seeded.isoformat(),
        window_minutes=window_minutes,
        moment=NOW,
    )
    later = NOW + timedelta(minutes=5)
    detected = budget_reset.observe(
        GROUP,
        resets_at=(later + timedelta(minutes=window_minutes)).isoformat(),
        window_minutes=window_minutes,
        moment=later,
    )
    assert detected["consumed"] is True, "the reading was not read as a reset"

    stream = tmp_path / "alpha.events"
    _sweep(PROJECTS[0], stream, seen=None, now=later + timedelta(minutes=1))
    rows = _reset_rows(_rows(stream))
    assert len(rows) == 1
    assert "detected from the window readings" in str(rows[0]["detail"])
    printed = _feed(_follower(SESSION_A), rows)
    assert len(_reset_rows(printed)) == 1


# ── a reset is not a lift ───────────────────────────────────────────────────


def test_a_pane_holding_a_lift_draws_the_reset_once_and_keeps_the_lift(tmp_path):
    """A reset for a group with a lift in force must not disturb the lift."""
    _announce()
    stream = tmp_path / "alpha.events"
    _sweep(PROJECTS[0], stream, seen=None)
    reset_row = _reset_rows(_rows(stream))[0]

    pane = _follower(SESSION_A)
    grant = _feed(pane, [_lift_row()])
    assert [row["to_state"] for row in grant] == [lift_watch.LIFT_GRANTED]

    assert len(_reset_rows(_feed(pane, [reset_row]))) == 1, (
        "a pane holding the group's lift never drew the reset"
    )
    assert len(_reset_rows(_feed(pane, [reset_row]))) == 0, (
        "the pane drew the reset twice"
    )

    # The lift family's memory is untouched: the baseline for the standing lift
    # is still held, and its end row is still drawn.
    assert _feed(pane, [_lift_row(kind="baseline")]) == [], (
        "the reset disturbed the pane's memory of the lift"
    )
    assert [row["to_state"] for row in _feed(pane, [_lift_row(ended="expired")])] == [
        lift_watch.LIFT_ENDED
    ]


def _stage_cells(row: dict) -> set[str]:
    """The stage cell of the row as the grid draws it.

    The grid drops the row's free text, so the token in the stage column is the
    stage word itself rather than a substring of the clause beside it.
    """
    grid = ticker.Ticker(width=WIDE).render(row)
    return {grid.split()[1]}


def test_a_reset_rows_stage_reads_limit_and_a_lift_rows_still_reads_lift(tmp_path):
    _announce()
    stream = tmp_path / "alpha.events"
    _sweep(PROJECTS[0], stream, seen=None)
    reset = _reset_rows(_rows(stream))[0]
    assert _stage_cells(reset) == {ticker.LIMIT_STAGE}
    assert _stage_cells(_lift_row()) == {ticker.LIFT_STAGE}


# ── only the project's own producer writes the stream ───────────────────────


def test_a_process_that_is_not_the_producer_writes_no_reset_row(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _announce()
    stream = tmp_path / "alpha.events"
    monkeypatch.setattr(runs, "_WATCH_STREAM_PRODUCERS", {})
    seen: set[str] | None = {"already-there"}
    assert recovery_watch._sweep_reset_rows(PROJECTS[0], seen) == seen
    assert _rows(stream) == [], "a non-producer wrote the project's stream"


def _producer(stream: Path) -> runs._WatchStreamProducer:
    """The seat's own record, as the seat registers one.

    A hand-built stand-in carries only the attributes a case happens to read,
    so a field the record gains later reaches the loop and its teardown as a
    missing attribute. Building the real record keeps the case measuring the
    sweep rather than the stub's shape.
    """
    return runs._WatchStreamProducer(
        path=stream, known={}, stall_window=runs.DEFAULT_WATCH_STALL_WINDOW
    )


def test_the_registered_producer_writes_the_row(tmp_path, monkeypatch):
    """The positive control for the guard above: it sees something present."""
    real_now = datetime.now(UTC)
    _announce(moment=real_now)
    stream = tmp_path / "alpha.events"
    monkeypatch.setattr(
        runs, "_WATCH_STREAM_PRODUCERS", {PROJECTS[0]: _producer(stream)}
    )
    monkeypatch.setattr(runs, "watch_stream_path", lambda stream_project: stream)
    seen = recovery_watch._sweep_reset_rows(PROJECTS[0], None)
    assert seen, "the registered producer announced nothing"
    assert len(_reset_rows(_rows(stream))) == 1


# ── the call site: the clock block, not the change gate ─────────────────────


class _StopTickingError(Exception):
    """Ends the producer once the case's steps have all run."""


def _drive_producer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    project: str,
    steps: list,
    marks: list[dict],
) -> None:
    """Run one producer with a simulated clock, stepping on each quiet boundary.

    Each boundary appends the tally of the pass that has just finished, so a
    case can name the pass a call happened on rather than only that it happened.
    """
    clock = {"t": 1_000.0, "i": 0}
    tally = {"list": 0, "reset": 0}
    real_list = recovery_watch.list_live
    real_reset = recovery_watch._sweep_reset_rows

    def counting_list(*args, **kwargs):
        tally["list"] += 1
        return real_list(*args, **kwargs)

    def counting_reset(sweep_project, seen):
        tally["reset"] += 1
        return real_reset(sweep_project, seen)

    monkeypatch.setattr(recovery_watch, "_utc_seconds", lambda: clock["t"])
    monkeypatch.setattr(recovery_watch, "list_live", counting_list)
    monkeypatch.setattr(recovery_watch, "_sweep_reset_rows", counting_reset)
    # The block's other neighbours are somebody else's measurement: stub them so
    # a failure here can only be the reset sweep's.
    monkeypatch.setattr(runs, "sweep_unfollowed_sessions", lambda _project: None)
    monkeypatch.setattr(recovery_watch, "_stop_delivered_reviews", lambda *a, **k: None)
    monkeypatch.setattr(model_catalogue, "check_model_catalogue", lambda _project: None)

    def sleeper(_interval: float) -> None:
        index = clock["i"]
        clock["i"] += 1
        marks.append(dict(tally))
        if index >= len(steps):
            raise _StopTickingError
        clock["t"] += recovery_watch.WATCHER_SWEEP_INTERVAL_SECONDS
        steps[index]()

    generator = recovery_watch.watch_ticker(project, poll_interval=0.0, sleeper=sleeper)
    with contextlib.suppress(_StopTickingError):
        list(generator)


def test_the_reset_sweep_runs_on_a_pass_where_the_change_gate_did_not_move(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """The call site is the capability: a still home, a passing clock, the row.

    A reset becomes due when an announced instant passes, so a sweep behind the
    change gate is never reached — every file the gate reads is still. This pins
    the call to the clock-driven block by finding a pass that neither listed the
    fleet nor took the gate, and requiring the reset row to have been written on
    it. Moving the call behind ``if sweep:`` reddens it.
    """
    project = PROJECTS[0]
    stream = tmp_path / "alpha.events"
    monkeypatch.setattr(runs, "_WATCH_STREAM_PRODUCERS", {project: _producer(stream)})
    monkeypatch.setattr(runs, "watch_stream_path", lambda _project: stream)
    _announce(moment=datetime.now(UTC))

    marks: list[dict] = []
    _drive_producer(
        monkeypatch,
        project=project,
        steps=[lambda: None, lambda: None, lambda: None],
        marks=marks,
    )

    assert len(marks) >= 3, marks
    quiet = [
        index
        for index in range(1, len(marks))
        if marks[index]["list"] == marks[index - 1]["list"]
        and marks[index]["reset"] > marks[index - 1]["reset"]
    ]
    assert quiet, (
        "the reset sweep never ran on a pass where the change gate was still, so "
        f"it is riding the gate rather than the clock: {marks}"
    )
    assert _reset_rows(_rows(stream)), "the due event never reached the stream"
