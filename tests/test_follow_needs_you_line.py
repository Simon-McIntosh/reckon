"""A follower's stream carries no standing line naming the runs it counts.

The pane carries counts on its rows -- ``w`` working, ``h`` halted, ``d``
delivered, ``q`` queued -- and a run that needs the coordinator is named, with
the command for it, by the obligations list. A standing summary line composed
beside those rows said the same thing a second time, on a cadence, under a
session host where every printed line starts a coordinator turn. It is gone, and
these cases hold it gone: however long the set holds, however it drains, and in
an arming that passes no flag at all.

The derivation behind it survives, because the counters and the obligations list
read it. So the cases here assert two things together: the stream emits nothing,
and the derivation still names exactly the runs the coordinator owes -- the
owning session's, never a peer's -- with the age and the stored-review mark a
reader of a row needs.

The set is the owning session's runs alone. A peer's blocked run in the same
fixture must never appear: the reader can do nothing about it, and naming it
would send a coordinator to a run another session already owns.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon import crew_follow_commands as follow
from reckon.crew import needs_you as need_module
from reckon.crew import runs

PROJECT = "needs-proj"
SESSION = "mine"
PEER = "peers"

POLL_SECONDS = 0.01
OBSERVE_SECONDS = 3.2

# The age the fixture's unpromoted run has held its state, measured from a
# transition stamped this far before the fixture is built, so the case asserts a
# real span rather than a literal that would have to be edited as time passes.
HELD_SECONDS = 2 * 60 * 60


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests, streams and the review store in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(
    home: Path,
    run_id: str,
    node: str,
    *,
    session: str,
    manifest_status: str | None = None,
    transitions: list[tuple[str, str]] | None = None,
    created_at: str | None = None,
) -> None:
    """Write one live pointer, its stream and its manifest the way a dispatch does."""
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "turn.started"})]
    for state, stamp in transitions or ():
        lines.append(
            json.dumps(
                {
                    "run_id": run_id,
                    "to_state": state,
                    "observed_at": stamp,
                    "legacy": False,
                }
            )
        )
    log.write_text("\n".join(lines) + "\n")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "node": {"id": node},
        "phase": "working",
        "created_at": created_at or runs._utc_now(),
        "log_path": str(log),
    }
    if manifest_status is not None:
        manifest = home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {node}\nstatus: {manifest_status}\nblockers: none\n"
        )
        pointer["manifest_path"] = str(manifest)
    crew._write_json(crew.pointer_path(run_id), pointer)


def _stamp_seconds_ago(seconds: float) -> str:
    """A UTC stamp this many seconds before now, in the stream's own spelling."""
    from datetime import UTC, datetime

    moment = datetime.fromtimestamp(time.time() - seconds, tz=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_stored_review(project: str, run_id: str) -> None:
    """File a staging review record against a reviewed run, as a reviewer does."""
    from reckon.crew import review as review_module

    store = review_module.review_store_root() / project
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{run_id}.json").write_text(
        json.dumps({"reviewed_run_id": run_id, "verdict": "pass"})
    )


class _Clock:
    """A follower's clock the case steps by hand.

    The follower reads its clock through one callable, so handing the generator
    this one makes a long wait observable in a test that lasts a fraction of a
    second.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _drive_at_own_pace(
    events: list[dict], stop: threading.Event, clock: _Clock, *, session: str
) -> threading.Thread:
    """Drive the follower on a stepped clock, as an arming with no flag runs."""

    def run() -> None:
        events.extend(
            follow._follow_watch_lines(
                PROJECT,
                session=session,
                poll_interval=POLL_SECONDS,
                stop=stop,
                sweep=None,
                clock=clock,
                sleeper=lambda _seconds: None,
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _advance(clock: _Clock, seconds: float, *, step: float = 1.0) -> None:
    """Move a stepped clock forward, yielding so the follower's passes can run.

    The spans these cases speak in are minutes and the follower's clock is the
    fake one, so a span is walked in slices: each slice is a moment the
    follower's pass can be measured at, and the sleep between slices is only
    there to let a pass happen.
    """
    remaining = float(seconds)
    while remaining > 0:
        clock.advance(min(step, remaining))
        remaining -= step
        time.sleep(0.005)


# The event name the standing summary line was written under. It is spelled
# here as a literal rather than read from the module, because the follower no
# longer writes it: a case asserts the stream carries no such row, and a
# restored emitter is exactly what the negative control reinstates.
NEEDS_YOU_EVENT = "follow.needs-you"


def _needs_you_lines(events: list[dict]) -> list[str]:
    return [
        str(event.get("line") or "")
        for event in events
        if event.get("event") == NEEDS_YOU_EVENT
    ]


def test_the_stream_never_carries_a_standing_line(home):
    """No standing summary line, however long the set holds.

    This is the case the negative control fails: restore the standing line and
    the 35 minutes below emit one line per beat of the interval, naming the
    unpromoted run, beside the rows the stream already carries for it.
    """
    _write_pointer(
        home,
        "r-own-unpromoted",
        "own-unpromoted",
        session=SESSION,
        manifest_status="complete",
        transitions=[
            ("working", _stamp_seconds_ago(HELD_SECONDS + 600)),
            ("completed_unpromoted", _stamp_seconds_ago(HELD_SECONDS)),
        ],
    )
    _write_pointer(
        home, "r-own-work", "own-node", session=SESSION, manifest_status="running"
    )
    _write_pointer(
        home,
        "r-peer-block",
        "peer-block-node",
        session=PEER,
        manifest_status="blocked",
    )

    fake = _Clock()
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive_at_own_pace(events, stop, fake, session=SESSION)
    try:
        time.sleep(0.2)
        _advance(fake, 35 * 60, step=10.0)
        assert _needs_you_lines(events) == [], _needs_you_lines(events)
    finally:
        stop.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def test_an_empty_set_is_never_announced(home):
    """An empty fleet draws nothing, however long the follower waits."""
    clock = _Clock()
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive_at_own_pace(events, stop, clock, session=SESSION)
    try:
        time.sleep(0.2)
        _advance(clock, 35 * 60, step=10.0)
        assert _needs_you_lines(events) == [], _needs_you_lines(events)
    finally:
        stop.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def test_a_draining_set_prints_nothing(home):
    """A set that empties after the pane was shown it full prints nothing.

    A run is owed, then promoted: the derivation drops it, and the stream never
    carried a sentence about it either way.
    """
    clock = _Clock()
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive_at_own_pace(events, stop, clock, session=SESSION)
    try:
        time.sleep(0.2)
        _write_pointer(
            home,
            "r-own",
            "own-node",
            session=SESSION,
            manifest_status="complete",
        )
        _advance(clock, 2.0)
        assert [
            row["run_id"]
            for row in need_module.needs_you_runs(PROJECT, session=SESSION)
        ] == ["r-own"]

        crew.pointer_path("r-own").unlink()
        _advance(clock, 2.0)
        assert need_module.needs_you_runs(PROJECT, session=SESSION) == []
        assert _needs_you_lines(events) == [], _needs_you_lines(events)

        # And then nothing, for as long as the fleet stays quiet.
        _advance(clock, 35 * 60, step=10.0)
        assert _needs_you_lines(events) == [], _needs_you_lines(events)
    finally:
        stop.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def test_an_arming_with_no_flag_carries_no_line(home):
    """An arming that passes no interval option prints nothing either.

    The fleet's followers arm through the command with no flags, so the default
    is what they run on. The clock is stepped by hand, which makes a long wait
    observable here in a fraction of a second.
    """
    _write_pointer(
        home, "r-own-work", "own-node", session=SESSION, manifest_status="running"
    )
    clock = _Clock()
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive_at_own_pace(events, stop, clock, session=SESSION)
    try:
        # Nothing prints while the fleet works.
        time.sleep(0.2)
        assert _needs_you_lines(events) == [], _needs_you_lines(events)

        # A run reaching a delivered state prints nothing either: its own row
        # carries the news, not a summary line over it and not a cadence.
        _write_pointer(
            home,
            "r-own-unpromoted",
            "own-unpromoted",
            session=SESSION,
            manifest_status="complete",
        )
        _advance(clock, 25 * 60, step=10.0)
        assert _needs_you_lines(events) == [], _needs_you_lines(events)
    finally:
        stop.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def test_the_rows_and_the_fleet_counters_agree(home):
    """The rows the derivation returns tally with the counters beside them.

    Both halves come from :mod:`reckon.crew.recovery_watch`: ownership from its
    partition and the working, blocked and unpromoted figures from its counter.
    This asserts that the two readers still agree about the same fixture rather
    than each about itself.
    """
    _write_pointer(
        home,
        "r-own-unpromoted",
        "own-unpromoted",
        session=SESSION,
        manifest_status="complete",
    )
    _write_pointer(
        home,
        "r-own-blocked",
        "own-blocked",
        session=SESSION,
        manifest_status="blocked",
    )
    _write_pointer(
        home, "r-own-work", "own-node", session=SESSION, manifest_status="running"
    )
    _write_pointer(
        home, "r-peer-block", "peer-block-node", session=PEER, manifest_status="blocked"
    )

    rows, counts = need_module.needs_you_rows(PROJECT, session=SESSION)
    tallies = {"unpromoted": 0, "blocked": 0}
    for row in rows:
        tallies[str(row["kind"])] += 1

    assert tallies["unpromoted"] == counts["unpromoted"], (rows, counts)
    assert tallies["blocked"] == counts["blocked"], (rows, counts)
    assert counts["working"] == 1, (rows, counts)
    assert not any(row["node"] == "peer-block-node" for row in rows), rows


def test_a_run_with_no_dated_transition_ages_from_its_creation(home):
    """A run the stream never dated must still age, and lead by that age.

    A stall writes no transition, and a run that has delivered then stopped has
    no ``blocked`` row to find, so the state-age lookup comes back empty and the
    row falls back to the run's own creation instant. Aged from the wrong field
    every such row reads ``0m`` and sorts to the bottom of its group by node
    name, which is precisely the row a coordinator is meant to see first.
    """
    _write_pointer(
        home,
        "r-own-recent",
        "own-recent",
        session=SESSION,
        manifest_status="complete",
        created_at=_stamp_seconds_ago(300),
    )
    _write_pointer(
        home,
        "r-own-old",
        "own-old",
        session=SESSION,
        manifest_status="complete",
        created_at=_stamp_seconds_ago(HELD_SECONDS),
    )

    rows, _counts = need_module.needs_you_rows(PROJECT, session=SESSION)

    assert [row["node"] for row in rows] == ["own-old", "own-recent"], rows
    assert rows[0]["age_seconds"] >= HELD_SECONDS - 60, rows[0]
    assert rows[1]["age_seconds"] < 600, rows[1]


def test_a_stored_review_marks_its_run(home):
    """An unpromoted run whose review already landed is marked ``reviewed``.

    Promotion unlinks the pointer and the session it captured, so a review read
    after the fact can no longer be answered by resuming its author; the mark is
    the live window's only warning that the chance to resume is going.
    """
    _write_pointer(
        home,
        "r-reviewed",
        "reviewed-node",
        session=SESSION,
        manifest_status="complete",
    )
    _write_stored_review(PROJECT, "r-reviewed")

    rows, _counts = need_module.needs_you_rows(PROJECT, session=SESSION)

    assert [row["node"] for row in rows] == ["reviewed-node"], rows
    assert rows[0]["reviewed"] is True, rows[0]


def test_the_derivation_names_the_owning_sessions_runs_only(home):
    """One fixture, one reader: the derivation names the owed set and no peer's run."""
    _write_pointer(
        home,
        "r-own-unpromoted",
        "own-unpromoted",
        session=SESSION,
        manifest_status="complete",
    )
    _write_pointer(
        home, "r-own-work", "own-node", session=SESSION, manifest_status="running"
    )
    _write_pointer(
        home,
        "r-peer-block",
        "peer-block-node",
        session=PEER,
        manifest_status="blocked",
    )

    clock = _Clock()
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive_at_own_pace(events, stop, clock, session=SESSION)
    try:
        time.sleep(0.2)
        _advance(clock, 20 * 60, step=10.0)
    finally:
        stop.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()

    derived_rows, _counts = need_module.needs_you_rows(PROJECT, session=SESSION)
    assert {str(row["node"]) for row in derived_rows} == {"own-unpromoted"}, (
        derived_rows
    )
    # The stream names no set at all: the names live in the obligations list.
    assert _needs_you_lines(events) == [], _needs_you_lines(events)
