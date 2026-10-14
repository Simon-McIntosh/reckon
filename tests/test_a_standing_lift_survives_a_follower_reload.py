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

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

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


def test_a_reload_holding_the_drawn_groups_draws_no_row_for_the_baseline(tmp_path):
    """The reader checkpoint an in-place reload hands across carries the groups.

    The pane drew the lift when it came into force, then the code moved and the
    image replaced itself. The producer's own reload re-derives a baseline for
    the stand, and the pane must draw nothing: the memory of the group travels
    in the checkpoint the reload hands across, restored before the first row is
    fed.
    """
    stream = tmp_path / "proj.events"
    stream.write_text("")
    baseline_row = _lift_row(GROUP, kind="baseline")
    assert baseline_row["to_state"] == ticker.LIFT_IN_FORCE

    # The carried checkpoint a reload hands over: the reported states and the
    # groups the pane laid on screen.
    resume_state = {
        "reported": {},
        "offset": 0,
        "stream_path": str(stream),
        "lift_groups": [GROUP],
    }
    mode, _offset, recorded, carried = _follow_resume_plan(
        PROJECT, SESSION, stream_path=stream, resume_state=resume_state
    )
    assert mode == "continue", mode

    path = ticker.PaneRowPath()
    path.reseed(recorded, lift_groups=carried)
    assert _printed(path, [baseline_row]) == [], (
        "an in-place reload re-announced a lift the pane already drew"
    )


def test_a_reload_with_no_carried_groups_draws_the_baseline(tmp_path):
    """The hold is the carried memory: without it the baseline is a fresh row."""
    stream = tmp_path / "proj.events"
    stream.write_text("")
    baseline_row = _lift_row(GROUP, kind="baseline")

    mode, _offset, recorded, carried = _follow_resume_plan(
        PROJECT,
        SESSION,
        stream_path=stream,
        resume_state={"reported": {}, "offset": 0, "stream_path": str(stream)},
    )
    assert mode == "continue", mode
    assert carried == (), "no groups were carried, so none are restored"

    path = ticker.PaneRowPath()
    path.reseed(recorded, lift_groups=carried)
    printed = _printed(path, [baseline_row])
    assert [row["to_state"] for row in printed] == [ticker.LIFT_IN_FORCE], (
        "a carried checkpoint naming no lift must still draw the standing lift"
    )


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

    path = ticker.PaneRowPath(lift_groups=groups)
    assert _printed(path, [_lift_row(GROUP, kind="baseline")]) == [], (
        "a re-arm re-announced a lift the stored history already shows"
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


def _history_row(*, state: str) -> dict:
    """One stored-history row for the standing group, as the follower writes it."""
    return {
        "text": f"row {state}",
        "at": 1.0,
        "run_id": "",
        "state": state,
        "group": GROUP,
    }
