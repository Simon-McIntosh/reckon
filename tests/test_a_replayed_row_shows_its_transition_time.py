"""A replayed row's clock names when its transition happened, not the attach.

A replay row is one a follower derives from what was already on disk when it
attached. The ``[replay]`` prefix already marks it, but its clock column still
carried the moment the follower printed it, so ``[replay] 12:30`` read as news
about a transition that had happened hours earlier. The time comes from the
event log now: the stamp of the run's last transition, whether that transition
is the one the live state shows or the last one of a run already terminal when
the pane attached. Where the log holds no such stamp the clock reads
``--:--:--`` rather than borrowing the attach time, because an unknown dressed
up as the current minute is the reading error this surface exists to prevent.
A live row, printed without the prefix, keeps the clock it had.

The cases drive the wired ``crew follow`` command against a temporary crew home
with the attach frozen and the zone forced to UTC, so a clock reading is the
stamp the fixture wrote.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_follow_commands
from reckon.crew import recovery_watch, runs

PROJECT = "replay-clock-proj"
SESSION = "s-replay-clock"

# The attach is frozen at 12:30:00; the transitions predate it by a day. A row
# drawn at the attach time therefore reads 12:30:00, and one drawn from the log
# reads the stamp the fixture wrote.
ATTACHED = datetime(2026, 11, 14, 12, 30, 0, tzinfo=UTC)
ATTACHED_STAMP = "2026-11-14T12:30:00Z"
TRANSITION_AT = datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC)
REVIEW_AT = datetime(2026, 11, 13, 8, 0, 0, tzinfo=UTC)
LIVE_AT = datetime(2026, 11, 14, 12, 31, 7, tzinfo=UTC)

RUN_TRANSITION = "r-clock-transition"
RUN_REVIEW = "r-clock-review"
RUN_UNSTAMPED = "r-clock-unstamped"
RUN_LIVE = "r-clock-arrives-live"

# What the pane's own memory names each run at, so each attach replays a move.
# The review run is left out: it is terminal at the attach, so the pane holds no
# word for it yet and its replay is a first sighting rather than a departure the
# row policy withholds until the run returns.
RECORD = {
    RUN_TRANSITION: "working",
    RUN_UNSTAMPED: "working",
}

ARM_LIFETIME = "1s"
PREFIX = "[replay] "


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home, and freeze the zone."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    original = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    monkeypatch.setattr(recovery_watch, "_utc_now", lambda: ATTACHED_STAMP)
    yield config_home
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


@pytest.fixture()
def follow_lines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the follower command's own rendered lines instead of writing them."""
    lines: list[str] = []

    def capture(line, *, stream=None):
        lines.append(line)

    monkeypatch.setattr(crew_follow_commands, "_echo_follow_line", capture)
    return lines


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _write_pointer(
    home: Path, run_id: str, *, created_at: datetime, role: str = "implement"
) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": run_id, "plan": "replay-clock-plan", "time_budget": "20m"},
            "phase": "working",
            "created_at": _iso(created_at),
            "role": role,
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _deliver(home: Path, run_id: str, status: str, *, at: datetime) -> None:
    """Write the worker's manifest, moving the run to the state it words."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    os.utime(manifest, (at.timestamp(), at.timestamp()))


def _transition(
    run_id: str,
    *,
    from_state: str,
    state: str,
    observed_at: datetime,
    role: str = "implement",
) -> dict:
    return {
        "project": PROJECT,
        "event": "transition",
        "run_id": run_id,
        "node": run_id,
        "session": SESSION,
        "role": role,
        "from_state": from_state,
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": _iso(observed_at),
        "legacy": False,
    }


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _store_fixture(home: Path) -> None:
    """The runs the store already holds when the pane attaches."""
    _write_pointer(home, RUN_TRANSITION, created_at=TRANSITION_AT)
    _deliver(home, RUN_TRANSITION, "failed", at=TRANSITION_AT)
    _write_pointer(home, RUN_REVIEW, created_at=REVIEW_AT, role="review")
    _deliver(home, RUN_REVIEW, "completed_unpromoted", at=REVIEW_AT)
    _write_pointer(home, RUN_UNSTAMPED, created_at=REVIEW_AT)
    _deliver(home, RUN_UNSTAMPED, "complete", at=REVIEW_AT)


def _log_fixture() -> list[dict]:
    """The transitions the log already held when the pane attached.

    The review run's last transition speaks the older word ``complete`` for the
    state its live row draws as delivered. That is the case the pane must date
    from the review's own last transition rather than from its attach.
    """
    return [
        _transition(
            RUN_REVIEW,
            from_state="in review",
            state="complete",
            observed_at=REVIEW_AT,
            role="review",
        ),
        _transition(
            RUN_TRANSITION,
            from_state="working",
            state="failed",
            observed_at=TRANSITION_AT,
        ),
    ]


def _arm(monkeypatch: pytest.MonkeyPatch, *, after: list[dict] | None = None) -> None:
    """Arm the follower once, appending ``after`` past its captured boundary."""
    if after:
        real_boundary = crew_follow_commands._follow_boundary
        written = {"done": False}

        def boundary(path):
            value = real_boundary(path)
            if not written["done"]:
                written["done"] = True
                _append_stream(Path(path), after)

            return value

        monkeypatch.setattr(crew_follow_commands, "_follow_boundary", boundary)
    result = CliRunner().invoke(
        cli.crew,
        [
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--lifetime",
            ARM_LIFETIME,
            "--no-color",
            "--width",
            "200",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


def _drawn(lines: list[str]) -> list[str]:
    """The drawn rows, without the pane's framing or the follower's end."""
    return [
        line
        for line in lines
        if line.strip() and "follower end" not in line and "── " not in line
    ]


def _naming(lines: list[str], run_id: str) -> list[str]:
    return [line for line in _drawn(lines) if run_id in line]


def test_a_replayed_row_shows_the_time_its_transition_was_recorded(
    home: Path, follow_lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_fixture(home)
    runs.write_delivered(PROJECT, SESSION, RECORD, at=_iso(ATTACHED))
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        with Path(seat["stream_path"]).open("a", encoding="utf-8") as handle:
            handle.writelines(f"{json.dumps(event)}\n" for event in _log_fixture())
        _arm(monkeypatch)

    transition = _naming(follow_lines, RUN_TRANSITION)
    assert transition, f"the replay draws the transition's row; got {follow_lines!r}"
    assert transition[-1].startswith(PREFIX + "09:00:00"), (
        f"a replayed transition's row carries the stamp the log holds for it; "
        f"got {transition[-1]!r}"
    )
    review = _naming(follow_lines, RUN_REVIEW)
    assert review, f"the replay draws the review's row; got {follow_lines!r}"
    assert review[-1].startswith(PREFIX + "08:00:00"), (
        f"a replayed row of a run already terminal at the attach carries the "
        f"stamp of its last transition; got {review[-1]!r}"
    )
    for line in transition + review:
        assert "12:30" not in line, (
            f"a replayed row never carries the attach time; got {line!r}"
        )


def test_a_transition_written_after_the_attach_keeps_its_own_clock(
    home: Path, follow_lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _transition(
        RUN_LIVE, from_state="working", state="failed", observed_at=LIVE_AT
    )
    _store_fixture(home)
    runs.write_delivered(PROJECT, SESSION, RECORD, at=_iso(ATTACHED))
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        with Path(seat["stream_path"]).open("a", encoding="utf-8") as handle:
            handle.writelines(f"{json.dumps(event)}\n" for event in _log_fixture())
        _arm(monkeypatch, after=[live])

    drawn = _naming(follow_lines, RUN_LIVE)
    assert drawn, (
        f"a transition written after the attach is drawn; got {follow_lines!r}"
    )
    assert not drawn[-1].startswith(PREFIX), (
        f"a transition written after the attach keeps the live format; "
        f"got {drawn[-1]!r}"
    )
    assert "12:31:07" in drawn[-1], (
        f"a live row carries its own observed time; got {drawn[-1]!r}"
    )


def test_a_replayed_run_with_no_recorded_transition_shows_no_clock(
    home: Path, follow_lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_fixture(home)
    runs.write_delivered(PROJECT, SESSION, RECORD, at=_iso(ATTACHED))
    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        with Path(seat["stream_path"]).open("a", encoding="utf-8") as handle:
            handle.writelines(f"{json.dumps(event)}\n" for event in _log_fixture())
        _arm(monkeypatch)

    drawn = _naming(follow_lines, RUN_UNSTAMPED)
    assert drawn, f"the replay draws the unstamped run's row; got {follow_lines!r}"
    assert "--:--:--" in drawn[-1], (
        f"a run with no recorded transition shows no clock rather than the attach "
        f"time; got {drawn[-1]!r}"
    )
    assert "12:30" not in drawn[-1], (
        f"an unrecorded transition is never dressed up as the attach time; "
        f"got {drawn[-1]!r}"
    )
