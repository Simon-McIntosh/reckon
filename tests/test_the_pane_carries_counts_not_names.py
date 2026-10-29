"""The pane carries counts on its rows and names no runs of its own.

A coordinator reads its fleet two ways. The pane draws one row per run with the
fleet's four counters -- ``w`` working, ``h`` halted, ``d`` delivered, ``q``
queued -- and every duty that still needs action is named, with the command for
it, by the obligations list. Two lines that copied the second onto the first
were measured as noise on 2026-10-29: the settle row's ``needs you: <names>``
tail, the end row's ``N runs need you: <run> (<node>)`` tail, and the station's
``the station's heartbeat is 90s old ... restart it`` line, printed 34 times in
one pane between 03:43 and 05:52 while the station it named was running.

So this file asserts the three settlements of that decision:

* the rows still count the runs, and no line naming them is composed;
* a settle still emits its event -- the follower's host retires the child on it.
  The event's line is empty, because a drained pane names nothing the
  obligations list does not;
* the lifetime end row keeps its re-arm instruction and names no run;
* a station whose heartbeat is 120 s old yields no sentence;
* ``crew follow --help`` lists no ``--remind``, whose promise was to re-emit a
  line the pane no longer prints.

The obligations view is the named list a coordinator works from, so the last
case asserts it still names every outstanding run with its command: removing
the two pane lines must not narrow the derivation behind it.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew
from reckon import crew_follow_commands as follow
from reckon.crew import follow_checkpoint, runs, station_store
from reckon.crew import needs_you as needs_you_module
from reckon.crew import obligations as obligations_module
from reckon.crew.station_subscriber import StationSubscriber
from reckon.crew_follow_commands import follower_row_path

PROJECT = "counts-proj"
SESSION = "counts-owner"
DELIVERED = "r-delivered"
HALTED = "r-halted"
WORKING = "r-working"

POLL = 0.005
DRAIN_BOUND = 20.0

# A moment far past every hold's window, so a row a hold withheld is released.
FAR_FUTURE = 10_000.0

# The station's stale-heartbeat age, well past the 90 s threshold.
STALE_HEARTBEAT_AGE = 120


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv(follow.FOLLOWER_SETTLE_GRACE_ENV, raising=False)
    return config_home


def _stamp(minutes_ago: float = 5.0) -> str:
    """A UTC stamp this many minutes before now, in the stream's own spelling."""
    moment = datetime.fromtimestamp(time.time() - minutes_ago * 60, tz=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_pointer(
    home: Path,
    run_id: str,
    node: str,
    *,
    session: str,
    phase: str,
    manifest_status: str,
) -> None:
    """Write one live pointer, its stream and its manifest the way a dispatch does."""
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"run_id": run_id, "to_state": phase, "observed_at": _stamp()})
        + "\n",
        encoding="utf-8",
    )
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {node}\nstatus: {manifest_status}\nblockers: none\n", encoding="utf-8"
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": runs._utc_now(),
            "manifest_path": str(manifest),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _transition(
    run_id: str,
    node: str,
    *,
    from_state: str,
    to_state: str,
    working: int,
    blocked: int,
    unpromoted: int,
) -> dict:
    """One transition row as the producer writes it, carrying the fleet counts."""
    return {
        "event": "transition",
        "project": PROJECT,
        "run_id": run_id,
        "node": node,
        "session": SESSION,
        "from_state": from_state,
        "to_state": to_state,
        "working": working,
        "blocked": blocked,
        "unpromoted": unpromoted,
        "waiting": 0,
        "observed_at": _stamp(1.0),
    }


def _rows(events) -> list[dict]:
    """Drive the follower's own row path over one stream, as the pane reads it."""
    path = follower_row_path(session=SESSION, lift_claim=lambda *_: True)
    printed: list[dict] = []
    now = 1_000.0
    for event in events:
        printed.extend(path.flush(now=now))
        printed.extend(path.feed(event, now=now))
        now += 250.0
    printed.extend(path.flush(now=FAR_FUTURE))
    return printed


def test_a_delivered_and_a_halted_run_are_counted_and_never_named(home: Path) -> None:
    """The two duties draw counted rows and no line names them.

    A delivered run and a halted run are the two states the pane's ``d`` and
    ``h`` columns count. Each draws its own row carrying the fleet's counts,
    and nothing composed for the pane names either -- the obligations list is
    where the names live.
    """
    printed = _rows(
        [
            _transition(
                DELIVERED,
                "delivered-node",
                from_state="working",
                to_state="completed_unpromoted",
                working=1,
                blocked=1,
                unpromoted=1,
            ),
            _transition(
                HALTED,
                "halted-node",
                from_state="working",
                to_state="blocked",
                working=0,
                blocked=1,
                unpromoted=1,
            ),
        ]
    )

    by_run = {str(row.get("run_id")): row for row in printed}
    assert DELIVERED in by_run, printed
    assert HALTED in by_run, printed
    # The counts ride the rows: a delivered run raises `d`, a halted one `h`.
    assert int(by_run[DELIVERED]["unpromoted"]) == 1, by_run[DELIVERED]
    assert int(by_run[HALTED]["blocked"]) == 1, by_run[HALTED]
    assert int(by_run[HALTED]["unpromoted"]) == 1, by_run[HALTED]

    # No row is a composed sentence naming a run: every printed row is a
    # transition, and none carries a `needs you` tail.
    assert all(row.get("event") == "transition" for row in printed), printed
    sentences = [str(row.get("line") or "") for row in printed]
    assert not any("needs you" in sentence for sentence in sentences), sentences


def test_a_settle_with_outstanding_runs_prints_no_line_and_still_emits(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drained pane whose session still owes a run says nothing, and exits.

    The follower's host retires the child on the settle event, not on its text,
    so the event is emitted with an empty line. The outstanding run is still
    owed -- the obligations list names it -- and it is exactly the run the
    settle row used to name.
    """
    monkeypatch.setattr(follow, "FOLLOWER_SETTLE_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(follow, "FOLLOWER_SETTLE_SAMPLE_SECONDS", 0.5)
    _write_pointer(
        home,
        WORKING,
        "working-node",
        session=SESSION,
        phase="working",
        manifest_status="running",
    )
    crew.list_live(project=PROJECT)

    events: list[dict] = []
    stop = threading.Event()
    stream = runs.watch_stream_path(PROJECT)
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text("", encoding="utf-8")

    def run() -> None:
        events.extend(
            follow._follow_watch_lines(
                PROJECT,
                session=SESSION,
                poll_interval=POLL,
                sleeper=lambda _seconds: time.sleep(POLL),
                stop=stop,
                sweep=None,
                settle=True,
                settle_interval=follow.FOLLOWER_SETTLE_GRACE_SECONDS,
            )
        )

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat):
        assert acquired
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            _wait_until(
                lambda: follow_checkpoint.exists(PROJECT, SESSION),
                timeout=10.0,
                description="the follower to attach",
            )
            # The session's last run delivered but not promoted: the pane has
            # no live work left to watch and still owes the coordinator a
            # promotion.
            _write_pointer(
                home,
                WORKING,
                "working-node",
                session=SESSION,
                phase="complete",
                manifest_status="complete",
            )
            _wait_until(
                lambda: any(
                    event.get("event") == follow.FOLLOWER_SETTLE_EVENT
                    for event in events
                ),
                timeout=DRAIN_BOUND,
                description="the pane to settle",
            )
        finally:
            stop.set()
            thread.join(5.0)

    settles = [
        event for event in events if event.get("event") == follow.FOLLOWER_SETTLE_EVENT
    ]
    assert len(settles) == 1, settles
    assert settles[0]["line"] == "", settles[0]["line"]

    owed = needs_you_module.needs_you_runs(PROJECT, session=SESSION)
    assert [row["run_id"] for row in owed] == [WORKING], owed
    assert owed[0]["kind"] == "unpromoted", owed


def test_the_lifetime_end_row_keeps_the_rearm_and_names_no_run(home: Path) -> None:
    """The end row carries how to continue watching, and no run.

    The row exists because the host will end the pane; its reader acts on the
    re-arm instruction. A run that needs the coordinator is named by the
    obligations list, so the end row names none.
    """
    _write_pointer(
        home,
        WORKING,
        "working-node",
        session=SESSION,
        phase="working",
        manifest_status="running",
    )

    event = follow._follower_end_event(
        PROJECT, session=SESSION, armed_seconds=1740.0, elapsed=1740.0
    )

    assert event["event"] == follow.FOLLOWER_END_EVENT, event
    assert "needs_you" not in event, event
    line = str(event["line"])
    assert line.startswith("follower end:"), line
    assert "re-arm with:" in line, line
    assert WORKING not in line, line
    assert "working-node" not in line, line
    assert "needs you" not in line, line


def test_a_stale_heartbeat_yields_no_station_line(tmp_path: Path) -> None:
    """A subscriber whose heartbeat is 120 s old prints no sentence.

    The station's liveness verdict belongs to the obligations hook, which is the
    one place a coordinator acts on it. A pane that repeated it on every slow
    tick told a reader to restart a station that was running.
    """
    store = station_store.StationStore(tmp_path / "station")
    store.heartbeat(now=datetime.now(tz=UTC) - timedelta(seconds=STALE_HEARTBEAT_AGE))
    subscriber = StationSubscriber(store, PROJECT, session=SESSION)

    stop = threading.Event()
    received: list[dict] = []

    def reader() -> None:
        received.extend(subscriber.wake(stop=stop, interval=0.001))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        time.sleep(0.3)
    finally:
        stop.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "the subscriber's loop never ended"
    stale = [event for event in received if "heartbeat" in str(event.get("line") or "")]
    assert stale == [], stale
    assert not hasattr(subscriber, "stale_line")


def test_the_follow_help_lists_no_remind_option() -> None:
    """`crew follow --help` offers no flag whose promise the pane cannot keep."""
    result = CliRunner().invoke(cli_module.main, ["crew", "follow", "--help"])

    assert result.exit_code == 0, result.output
    assert "--remind" not in result.output, result.output


def test_the_obligations_rows_name_every_outstanding_run_with_its_command(
    home: Path,
) -> None:
    """Removing the pane's lines must not narrow the list a coordinator works from.

    A delivered run and a halted run are both duties, and each row carries the
    command that answers it.
    """
    _write_pointer(
        home,
        DELIVERED,
        "delivered-node",
        session=SESSION,
        phase="working",
        manifest_status="complete",
    )
    _write_pointer(
        home,
        HALTED,
        "halted-node",
        session=SESSION,
        phase="working",
        manifest_status="blocked",
    )

    report = obligations_module.obligations(PROJECT, SESSION)
    named = {
        str(item.get("run_id")): str(item.get("next_command") or "")
        for item in report["obligations"]
    }

    assert DELIVERED in named, named
    assert HALTED in named, named
    for run_id in (DELIVERED, HALTED):
        assert named[run_id].strip(), (run_id, named)


def _wait_until(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"timed out after {timeout:g}s waiting for {description}")
