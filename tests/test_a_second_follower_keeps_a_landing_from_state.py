"""A follower armed after a settle continues the pane's words.

A pane is not one process. A host restarts a session's follower whenever its
last one settles or ends, and the next image builds a fresh row path and a fresh
grid. Both memories — the state the pane last showed each run, and whether the
row that showed it was carried — live in that process, so a re-arm restores them
from the checkpoint the previous image wrote.

The defect these cases measure was reported from a live pane. After the settle
at 21:12 the host armed the next follower, and its first row for a run the pane
had shown as delivered two hours earlier read ``new → landed``. The checkpoint
named the run; the arming that read it threw the memory away, because only an
in-place reload restored it and a re-arm is not a reload — it is a new process.

So the run below is drawn as ``delivered`` by one arming, that arming settles,
and a second arming is given the landing while it is attached. Its first row for
the run must continue from the word the pane already carries.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_follow_commands
from reckon.crew import follow_checkpoint, runs

PROJECT = "second-follower-state"
SESSION = "s-second-follower"
RUN = "r-second-follower-a"

# Long enough for the second arming to reach its read pass on a loaded host,
# short enough that the case stays quick.
SECOND_ARM_LIFETIME = "3s"


class _Clock:
    """A clock the case advances, so a hold window costs no wall time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture(autouse=True)
def _a_live_owner(monkeypatch: pytest.MonkeyPatch):
    """Isolate both armings from an owner stamped into the ambient environment."""
    previous = runs._RESOLVED_FOLLOWER_OWNER.resolved
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    monkeypatch.setattr(runs._RESOLVED_FOLLOWER_OWNER, "resolved", None)
    yield
    monkeypatch.setattr(runs._RESOLVED_FOLLOWER_OWNER, "resolved", previous)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_pointer(home: Path, *, phase: str, status: str) -> None:
    """One live pointer and the manifest that words its state."""
    manifest = home / "manifests" / f"{RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {RUN}\nstatus: {status}\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": RUN, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": _now(),
            "manifest_path": str(manifest),
            "process_alive": None,
        },
    )


def _append(stream_path: Path, *, from_state: str, to_state: str) -> None:
    """Append one transition, as the producer writes it."""
    row = {
        "project": PROJECT,
        "event": "transition",
        "run_id": RUN,
        "node": RUN,
        "session": SESSION,
        "from_state": from_state,
        "to_state": to_state,
        "observed_at": _now(),
        "working": 0,
        "blocked": 0,
        "unpromoted": 1 if to_state == "completed_unpromoted" else 0,
    }
    with stream_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{json.dumps(row)}\n")


def _arm(clock: _Clock) -> tuple[list[dict], threading.Thread, threading.Event]:
    """Run one arming until it settles, and return every event it emitted."""
    events: list[dict] = []
    stop = threading.Event()

    def run() -> None:
        events.extend(
            crew_follow_commands._follow_watch_lines(
                PROJECT,
                session=SESSION,
                poll_interval=0.01,
                settle_interval=0.01,
                sweep=None,
                settle=True,
                stop=stop,
                clock=clock,
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return events, thread, stop


def _settled(events: list[dict]) -> bool:
    return any(
        event.get("event") == crew_follow_commands.FOLLOWER_SETTLE_EVENT
        for event in events
    )


def _follow_lines() -> str:
    """Arm the real follower command once, as a host arms its next follower."""
    result = CliRunner().invoke(
        cli.crew,
        [
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--lifetime",
            SECOND_ARM_LIFETIME,
            "--no-color",
            "--width",
            "200",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def _naming(lines: str) -> list[str]:
    """The drawn rows that name the run."""
    return [line for line in lines.splitlines() if "second-follower-a" in line]


def test_a_second_follower_continues_the_landing_from_the_panes_word(
    home: Path,
) -> None:
    """The landing continues from ``delivered``, not from ``new``."""
    clock = _Clock()
    _write_pointer(home, phase="running", status="running")
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired, "the case holds the project's watch claim"
        stream_path = Path(seat["stream_path"])
        crew.list_live(project=PROJECT)

        events, thread, stop = _arm(clock)
        try:
            # The run delivers while the first arming is attached, so the pane
            # is shown the word the second arming must continue from.
            time.sleep(0.4)
            _append(
                stream_path,
                from_state="running",
                to_state="completed_unpromoted",
            )
            time.sleep(0.4)
            # A delivery is a coordinator row, held for its settle window; a
            # later line releases it and the row is drawn then.
            clock.value += 30.0
            _append(
                stream_path,
                from_state="completed_unpromoted",
                to_state="completed_unpromoted",
            )
            time.sleep(0.4)
            # The session drains, so the first arming settles and ends.
            _write_pointer(home, phase="complete", status="complete")
            clock.value += 200.0
            # The empty reading is taken on one sample and the grace passes on
            # the next, so the clock moves twice.
            time.sleep(0.4)
            clock.value += 1.0
            thread.join(20.0)
            assert not thread.is_alive(), "the first arming did not settle"
        finally:
            stop.set()
            thread.join(5.0)

        assert _settled(events), f"the first arming never settled: {events!r}"
        drawn = [
            str(event.get("to_state"))
            for event in events
            if event.get("run_id") == RUN and event.get("to_state")
        ]
        assert "completed_unpromoted" in drawn, (
            f"the first arming draws the delivery; got {events!r}"
        )

        record = follow_checkpoint.read(PROJECT, SESSION)
        assert record, "the first arming leaves its place behind"
        assert record.get("reported", {}).get(RUN) == "completed_unpromoted", (
            f"the checkpoint carries the state the pane showed; got {record!r}"
        )

        # The landing arrives while the second arming is attached, and the
        # promotion takes the run out of the live fleet.
        lines: list[str] = []

        def second_arm() -> None:
            lines.append(_follow_lines())

        # The promotion takes the run out of the live fleet, so the second
        # arming draws no baseline row for it: the landing is the first thing
        # it draws for this run.
        crew.pointer_path(RUN).unlink(missing_ok=True)
        attached = threading.Thread(target=second_arm, daemon=True)
        attached.start()
        time.sleep(1.0)
        _append(
            stream_path,
            from_state="completed_unpromoted",
            to_state="promoted",
        )
        attached.join(30.0)
        assert not attached.is_alive(), "the second arming did not end"

    output = lines[0] if lines else ""
    rows = _naming(output)
    assert rows, f"the second arming draws no row for the run: {output!r}"
    assert any("delivered" in row and "landed" in row for row in rows), (
        f"the landing continues the pane's word; got {rows!r}"
    )
