"""A follower stands a line naming the runs that need the coordinator, and repeats it.

The pane a coordinator reads is quiet for long stretches, so a follower that has
died and one watching a healthy idle fleet look the same. The standing line
answers both: it names every owned run whose state needs action, and it re-emits
on a cadence whether or not the set is empty, so a silent pane is itself a fact
about the follower rather than about the fleet.

The line is one derivation, in ``reckon/crew/needs_you.py``, because the follower
prints it while it lives and again as the run it names is delivered; two
derivations would let the pane and its closing row disagree about which runs
needed attention. So the last case here asserts the two agree over one fixture
rather than asserting each alone.

The list reads the owning session's runs only. A peer's blocked run in the same
fixture must never appear: the reader can do nothing about it, and naming it
would send a coordinator to a run another session already owns.
"""

from __future__ import annotations

import json
import re
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

# One second, so a case finishes in the time it takes to observe a handful of
# lines rather than the production ten minutes.
REMIND_SECONDS = 1.0
POLL_SECONDS = 0.01
OBSERVE_SECONDS = 3.2

# The age the fixture's unpromoted run has held its state, measured from a
# transition stamped this far before the fixture is built, so the case asserts a
# real span rather than a literal that would have to be edited as time passes.
HELD_SECONDS = 2 * 60 * 60

# The age token the pane prints for a span in hours; minutes alone below one.
_AGE_TOKEN = re.compile(r"\b\d+h\d{2}m\b|\b\d+m\b")

# An entry in a composed line: the node, then the follower's age token, then the
# ``reviewed`` mark when a stored review is already on file.
_ENTRY_TOKEN = re.compile(r"(\S+) (?:\d+h\d{2}m|\d+m)(?: reviewed)?")


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
        "created_at": runs._utc_now(),
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
    (store / f"{run_id}.json").write_text(json.dumps({"verdict": "pass"}))


class _Clock:
    """A follower's clock the case steps by hand.

    The follower reads its clock through one callable, so handing the generator
    this one makes the arming, a set change and a ten-minute cadence observable
    in a test that lasts a fraction of a second.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _drive(
    events: list[dict], stop: threading.Event, *, session: str
) -> threading.Thread:
    """Consume the follower's watch generator on a thread, ended by ``stop``."""

    def run() -> None:
        events.extend(
            follow._follow_watch_lines(
                PROJECT,
                session=session,
                poll_interval=POLL_SECONDS,
                stop=stop,
                sweep=None,
                remind=REMIND_SECONDS,
                lifetime=OBSERVE_SECONDS * 2,
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


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
                remind=follow._needs_remind_seconds(None),
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _wait_for_lines(events: list[dict], count: int, *, bound: float = 5.0) -> list[str]:
    """Wait until this many lines have been emitted, and return them.

    The lines are re-read from the event list on every pass: the follower
    appends as it yields, so a snapshot taken once would never grow.
    """
    deadline = time.time() + bound
    while time.time() < deadline and len(_needs_you_lines(events)) < count:
        time.sleep(0.01)
    return _needs_you_lines(events)


def _nodes_in_line(line: str) -> set[str]:
    """The node names a composed line prints, read back off the line itself.

    Each entry in a group reads ``<node> <age>[ reviewed]``, so the token before
    the age is the node. Parsing the pane's own text is the point: comparing the
    emitter's helper with the derivation's helper compares one function with
    itself and cannot fail.
    """
    return {match.group(1) for match in _ENTRY_TOKEN.finditer(line)}


def _needs_you_lines(events: list[dict]) -> list[str]:
    return [
        str(event.get("line") or "")
        for event in events
        if event.get("event") == follow.FOLLOWER_NEEDS_YOU_EVENT
    ]


def _run_needs_you(
    home: Path, *, session: str = SESSION
) -> tuple[list[str], list[dict]]:
    """Drive one follower for the observation window and return its lines and rows.

    A live producer is never started: the wait pass composes the standing line
    on its own, and the run does not settle, so the follower is left running
    until the stop event ends it.
    """
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive(events, stop, session=session)
    try:
        time.sleep(OBSERVE_SECONDS)
    finally:
        stop.set()
        thread.join(timeout=5.0)
    rows = [e for e in events if e.get("event") == follow.FOLLOWER_NEEDS_YOU_EVENT]
    return _needs_you_lines(events), rows


def test_the_line_names_your_unpromoted_run_and_repeats(home):
    """The first case: a standing line, repeated, naming only your own needs."""
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

    lines, _rows = _run_needs_you(home)

    assert len(lines) >= 2, f"expected the cadence to repeat the line: {lines!r}"
    first = lines[0]
    assert first.startswith("needs you:"), first
    assert "own-unpromoted" in first, first
    assert "unpromoted" in first, first
    assert _AGE_TOKEN.search(first), f"no age token in: {first!r}"
    assert not any("peer-block-node" in line for line in lines), lines
    assert not any("blocked" in line for line in lines), lines


def test_a_quiet_pane_still_beats(home):
    """With nothing needing action the cadence line still appears and says so."""
    _write_pointer(
        home, "r-own-work", "own-node", session=SESSION, manifest_status="running"
    )

    lines, _rows = _run_needs_you(home)

    assert len(lines) >= 2, f"expected the heartbeat to repeat: {lines!r}"
    assert all(line == "yours: 1 working · nothing needs you" for line in lines), lines


def test_an_arming_with_no_flag_carries_the_default_cadence(home):
    """An arming that passes no ``--remind`` still gets the line and the beat.

    The flag is an override, not a switch: the fleet's followers arm through the
    command with no interval, so the default has to be what they run on. The
    clock is stepped by hand, which makes a ten-minute cadence observable here
    in a fraction of a second.
    """
    assert follow._needs_remind_seconds(None) == need_module.DEFAULT_REMIND_SECONDS

    _write_pointer(
        home, "r-own-work", "own-node", session=SESSION, manifest_status="running"
    )
    clock = _Clock()
    events: list[dict] = []
    stop = threading.Event()
    thread = _drive_at_own_pace(events, stop, clock, session=SESSION)
    try:
        # The arming's own row already names the fleet as it stands, so the
        # standing line is not news until something moves.
        time.sleep(0.2)
        assert _needs_you_lines(events) == [], _needs_you_lines(events)

        # A run reaching a delivered state is a change of the set.
        _write_pointer(
            home,
            "r-own-unpromoted",
            "own-unpromoted",
            session=SESSION,
            manifest_status="complete",
        )
        clock.advance(2.0)
        changed = _wait_for_lines(events, 1)
        assert changed, "no line after the set changed"
        assert "own-unpromoted" in changed[0], changed

        # Then nothing moves, and the default cadence beats anyway.
        clock.advance(need_module.DEFAULT_REMIND_SECONDS + 2.0)
        beaten = _wait_for_lines(events, 2)
        assert len(beaten) >= 2, f"the default cadence did not repeat: {beaten!r}"
        assert "own-unpromoted" in beaten[1], beaten
    finally:
        stop.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def test_the_line_and_the_fleet_counters_agree(home):
    """The rows the line prints tally with the counters the fleet derives.

    Both halves come from :mod:`reckon.crew.recovery_watch`: ownership from its
    partition and the working, blocked and unpromoted figures from its counter.
    The line reads the same bucket constants, so this asserts that the two
    readers still agree about the same fixture rather than each about itself.
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

    lines, _rows = _run_needs_you(home)

    assert lines, "no standing line was emitted"
    assert "reviewed-node" in lines[0], lines[0]
    assert "reviewed" in lines[0], lines[0]


def test_the_follower_and_the_derivation_name_the_same_runs(home):
    """One fixture, two readers: the pane's line and the module name the same set."""
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
        home, "r-peer-block", "peer-block-node", session=PEER, manifest_status="blocked"
    )

    lines, _rows = _run_needs_you(home)

    derived_rows, _counts = need_module.needs_you_rows(PROJECT, session=SESSION)
    derived_nodes = {str(row["node"]) for row in derived_rows}
    assert derived_nodes == {"own-unpromoted"}, derived_rows

    # Read the node names back off the pane's own text. Comparing the follower's
    # wrapper with the module's helper compares one function with itself, since
    # the wrapper delegates; the emitted line is what a reader actually sees.
    emitted_nodes: set[str] = set()
    for line in lines:
        emitted_nodes |= _nodes_in_line(line)
    assert emitted_nodes == derived_nodes, (emitted_nodes, derived_nodes, lines)
