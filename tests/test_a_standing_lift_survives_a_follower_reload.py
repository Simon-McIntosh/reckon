"""A pane's memory of the lift groups it drew survives its own reload.

The row policy holds a baseline lift row for a group the pane already drew, so a
producer that restarts adds no row for a stand that is still in force. That set
lives only in the follower process, so an in-place reload — the reader checkpoint
handed across an image replacement — lost it, and the next producer pass
re-announced the stand as ``new → working``. These drive the memory across the
two carriers the follower restores it from: the checkpoint an in-place reload
hands across, and the stored history a re-arm reads.

The rows are the producer's own shape, built through ``lift_watch``; the memory
is restored through the follower's own seams — ``_follow_resume_plan`` for the
carried checkpoint, ``follow_checkpoint.seed_lift_groups`` for the history — so a
measurement counts the rows the pane prints rather than a fixture's opinion.

The declared negative control — leaving the lift groups out of the carried
memory — is applied on a scratch copy, not here; these tests are the green arm,
and that mutation must turn the reload case red.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from reckon import cli
from reckon.crew import budget_lift as bl
from reckon.crew import follow_checkpoint, runs, ticker
from reckon.crew_follow_commands import _follow_resume_plan
from tests.test_a_standing_lift_is_announced_once import (
    GROUP,
    PROJECT,
    _lift_row,
)

SESSION = "sess-reload"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the config home at a temp dir; prove the real records are untouched."""
    real_lifts = bl.lifts_path()
    real_stream = runs.watch_stream_path(PROJECT)
    before_lifts = real_lifts.read_bytes() if real_lifts.exists() else None
    before_stream = real_stream.read_bytes() if real_stream.exists() else None
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    monkeypatch.delenv(bl.RUN_ID_ENV, raising=False)
    yield tmp_path
    after_lifts = real_lifts.read_bytes() if real_lifts.exists() else None
    after_stream = real_stream.read_bytes() if real_stream.exists() else None
    assert after_lifts == before_lifts, "a test wrote the live budget-lifts.json"
    assert after_stream == before_stream, "a test wrote the live watch stream"


def _printed(path: ticker.PaneRowPath, rows: Sequence[dict], *, start: float = 1000.0):
    """Feed rows to one pane in order and return the rows it printed."""
    out: list[dict] = []
    now = start
    for row in rows:
        out.extend(path.feed(row, now=now))
        now += 1.0
    return out


# ── the in-place reload carries the drawn groups ────────────────────────────


def test_a_reload_holding_the_drawn_groups_draws_no_row_for_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader checkpoint an in-place reload hands across carries the groups.

    The pane drew the lift when it came into force, then the code moved and the
    image replaced itself. The producer's own reload re-derives a baseline for
    the stand, and the pane must draw nothing: the memory of the group travels
    in the checkpoint the reload hands across, restored before the first row is
    fed.
    """
    rows = _reload_rows(monkeypatch, carried=[GROUP])
    assert _lift_rows_drawn(rows) == [], (
        "an in-place reload re-announced a lift the pane already drew"
    )


def test_a_reload_carrying_no_groups_draws_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness is live, and the hold is the carried memory alone.

    A reload whose checkpoint names no lift has nothing to hold the producer's
    baseline with, so the same drive draws the row. Without this the case above
    would pass for an arming that never received the row at all.
    """
    rows = _reload_rows(monkeypatch, carried=[])
    assert [row["to_state"] for row in _lift_rows_drawn(rows)] == [
        ticker.LIFT_IN_FORCE
    ], f"the baseline row never reached the pane: {rows}"


def test_the_carried_groups_travel_back_with_the_continuation(tmp_path):
    """The checkpoint's groups are what the reload plan hands to the row path.

    The two cases above measure the pane; this one measures the carrier, so a
    checkpoint that reads back empty is caught where the memory is read rather
    than only where a row would have appeared.
    """
    stream = tmp_path / "proj.events"
    stream.write_text("")
    resume_state = {
        "reported": {},
        "offset": 0,
        "stream_path": str(stream),
        "lift_groups": [GROUP],
    }
    mode, _offset, _recorded, carried = _follow_resume_plan(
        PROJECT, SESSION, stream_path=stream, resume_state=resume_state
    )
    assert mode == "continue", mode
    assert carried == (GROUP,), carried

    mode, _offset, _recorded, carried = _follow_resume_plan(
        PROJECT,
        SESSION,
        stream_path=stream,
        resume_state={"reported": {}, "offset": 0, "stream_path": str(stream)},
    )
    assert mode == "continue", mode
    assert carried == (), "no groups were carried, so none are restored"


# ── the re-arm restores the groups from the stored history ──────────────────


def test_a_rearm_reads_the_drawn_groups_from_stored_history():
    """A re-arm has no carried checkpoint, so the groups come from the log.

    The pane's stored history holds the rows it drew, so the groups in force are
    the grants on that log not yet ended. Restored before the replay feeds the
    fleet, the producer's baseline adds no row.
    """
    history = [
        _history_row(state=ticker.LIFT_GRANTED),
        _history_row(state=ticker.LIFT_IN_FORCE),
    ]
    for row in history:
        follow_checkpoint.append_history(
            PROJECT,
            SESSION,
            text=row["text"],
            at=row["at"],
            run_id=row["run_id"],
            state=row["state"],
            group=row["group"],
        )

    stored = follow_checkpoint.read_history(PROJECT, SESSION)
    groups = follow_checkpoint.seed_lift_groups(stored)
    assert groups == {GROUP}, groups
    assert [row["group"] for row in stored] == [GROUP, GROUP], (
        "the stored rows do not name the group, so the read above cannot be read"
    )


def test_an_end_in_the_history_forgets_the_group():
    """The log read is ordered: a grant then an end leaves no group in force."""
    follow_checkpoint.append_history(
        PROJECT, SESSION, text="grant", at=1.0, state=ticker.LIFT_GRANTED, group=GROUP
    )
    follow_checkpoint.append_history(
        PROJECT, SESSION, text="end", at=2.0, state=ticker.LIFT_ENDED, group=GROUP
    )
    groups = follow_checkpoint.seed_lift_groups(
        follow_checkpoint.read_history(PROJECT, SESSION)
    )
    assert groups == set(), groups


# ── the unchanged cases ──────────────────────────────────────────────────────


def test_a_pane_attaching_for_the_first_time_draws_the_standing_lift_once():
    """A first attach has no history, so the standing lift shows once."""
    assert follow_checkpoint.seed_lift_groups([]) == set()
    path = ticker.PaneRowPath()
    printed = _printed(path, [_lift_row(GROUP, kind="baseline")])
    assert [row["to_state"] for row in printed] == [ticker.LIFT_IN_FORCE]
    assert _printed(path, [_lift_row(GROUP, kind="baseline")]) == [], (
        "the pane drew the same standing lift twice"
    )


def test_a_grant_and_an_end_after_the_reload_each_draw_one_row():
    """The hold survives but a grant and an end are still news after a reload."""
    path = ticker.PaneRowPath(lift_groups=[GROUP])
    grant = _lift_row(GROUP)  # a transition with no end is a grant
    assert grant["to_state"] == ticker.LIFT_GRANTED
    assert [row["to_state"] for row in _printed(path, [grant])] == [ticker.LIFT_GRANTED]

    end = _lift_row(GROUP, ended="cleared")
    assert end["to_state"] == ticker.LIFT_ENDED
    assert [row["to_state"] for row in _printed(path, [end])] == [ticker.LIFT_ENDED]


# ── the follower's own re-arm arm ────────────────────────────────────────────


def _plant_seat() -> None:
    """Leave the producer's own seat record on disk, as a live producer does."""
    lock = runs.watch_lock_path(PROJECT)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "started_at": runs._utc_now(),
                "stream_path": str(runs.watch_stream_path(PROJECT)),
                "log_path": str(runs.watch_log_path(PROJECT)),
                "reckon_version": runs.__version__,
                "code_stamp": runs.follower_code_stamp(),
            }
        )
    )


def _drive_arm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resume: dict | None,
    reloaded_in_place: bool,
    stop_after: int = 4,
) -> list[dict]:
    """Drive one arming of the follower's own generator, returning its rows.

    ``resume`` is the payload an in-place reload hands to its replacement, and
    its absence is what makes this a re-arm: the follower then restores what it
    remembers from its own stored history instead of from a carried checkpoint.

    The producer's own baseline row for the standing lift is appended while the
    arming waits, so it arrives through the follower's read loop and reaches the
    pane exactly as an ordinary transition does. The harness is proved live by
    the cases that carry no memory at all: those must draw that row.
    """
    monkeypatch.setattr(runs, "producer_live", lambda project: True)
    monkeypatch.setattr(
        runs,
        "follower_owner",
        lambda: (os.getpid(), runs._process_start_time(os.getpid())),
    )
    _plant_seat()
    stream_path = runs.watch_stream_path(PROJECT)
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    stream_path.write_text("", encoding="utf-8")
    stop = threading.Event()
    appended = {"done": False}
    baseline_row = _lift_row(GROUP, kind="baseline")

    def on_poll(_payload: dict) -> None:
        if appended["done"]:
            return
        appended["done"] = True
        runs._append_watch_lines(stream_path, [baseline_row])

    sleeps = {"count": 0}

    def sleeper(_seconds: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] >= stop_after:
            stop.set()

    return list(
        cli._follow_watch_lines(
            PROJECT,
            session=SESSION,
            poll_interval=0.001,
            sleeper=sleeper,
            stop=stop,
            on_poll=on_poll,
            sweep=None,
            resume=resume,
            reloaded_in_place=reloaded_in_place,
        )
    )


def _rearm_rows(monkeypatch: pytest.MonkeyPatch, *, stop_after: int = 4) -> list[dict]:
    """Drive the follower's re-arm arm: no in-place checkpoint, stored history."""
    return _drive_arm(monkeypatch, resume=None, reloaded_in_place=False)


def _reload_rows(
    monkeypatch: pytest.MonkeyPatch,
    *,
    carried: Sequence[str],
    stop_after: int = 4,
) -> list[dict]:
    """Drive the follower's in-place reload arm with the groups it carries."""
    stream_path = runs.watch_stream_path(PROJECT)
    resume = {
        "offset": 0,
        "stream_path": str(stream_path),
        "reported": {},
        "lift_groups": list(carried),
        "layout": cli._ticker_layout_signature(),
    }
    return _drive_arm(
        monkeypatch, resume=resume, reloaded_in_place=True, stop_after=stop_after
    )


def _lift_rows_drawn(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("to_state") in ticker.LIFT_STATES]


def test_a_rearm_restores_the_groups_from_stored_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follower's own re-arm arm holds the producer's baseline for the stand.

    The producer restarts on every reload and re-derives a baseline row for
    every lift already in force, and a re-armed follower has no carried
    checkpoint — only the history it wrote while it was attached. Restored
    before the read loop feeds that baseline, the pane draws nothing for a stand
    it already showed.
    """
    follow_checkpoint.append_history(
        PROJECT,
        SESSION,
        text="grant",
        at=1.0,
        state=ticker.LIFT_GRANTED,
        group=GROUP,
    )
    rows = _rearm_rows(monkeypatch)
    assert _lift_rows_drawn(rows) == [], (
        "the re-armed follower re-announced a lift its stored history shows in force"
    )


def test_a_rearm_without_stored_history_draws_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness is live: with no history the same drive draws that row.

    Without this, the case above would pass for an arming that never received
    the row at all — an absence read as a hold. The history is the only
    difference between the two cases.
    """
    rows = _rearm_rows(monkeypatch)
    assert [row["to_state"] for row in _lift_rows_drawn(rows)] == [
        ticker.LIFT_IN_FORCE
    ], f"the baseline row never reached the pane: {rows}"


def _history_row(*, state: str) -> dict:
    """One stored-history row for the standing group, as the follower writes it."""
    return {
        "text": f"row {state}",
        "at": 1.0,
        "run_id": "",
        "state": state,
        "group": GROUP,
    }
