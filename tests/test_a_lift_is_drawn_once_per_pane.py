"""A lift is drawn once per session pane, however many projects it follows.

A session host runs one ``crew follow`` per project the session follows, and
they all write the session's one pane. Every project's producer sweeps the whole
workstation lift document into its own event stream, so the same grant reaches
the pane once per project followed. Each follower kept its own memory of the
lifts it had drawn, so a pane following three projects drew one grant three
times — the noise a lead read on 2026-10-14, at 18:29:46Z.

These drive the follower's own row path — the same object the follower feeds —
with the lift rows built by ``lift_watch``, and count the rows the pane prints.
The pane's shared claim lives under a temporary crew home, and every fixture
asserts afterwards that the real crew home is untouched.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import lift_watch, runs
from reckon.crew_follow_commands import (
    LIFT_CLAIM_MAX_AGE_SECONDS,
    follower_row_path,
    lift_claim_dir,
    take_lift_claim,
)

SESSION = "s-power"
OTHER_SESSION = "s-other"
GROUP = "codex-sub"
LIFT_ID = "codex-sub-1792002586-64d825"
SECOND_LIFT_ID = "codex-sub-1792003000-aaaaaa"
PROJECTS = ("alpha", "beta", "gamma")
NOW = datetime(2026, 10, 14, 18, 30, tzinfo=UTC)


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


def _lift_row(
    project: str,
    *,
    kind: str = "transition",
    ended: str | None = None,
    lift_id: str = LIFT_ID,
    observed: str | None = None,
) -> dict:
    """One lift row in the producer's own shape, built by ``lift_watch``."""
    lift = {
        "group": GROUP,
        "form": "five_hour",
        "pace_multiple": 1.8,
        "scope": "global",
        "id": lift_id,
    }
    row = lift_watch._lift_row(project, lift, kind=kind, ended=ended)
    if observed is not None:
        row["observed_at"] = observed
    return row


def _follower(session: str | None = SESSION):
    return follower_row_path(session=session)


def _feed(path, rows: Sequence[dict], *, start: float = 1000.0) -> list[dict]:
    """Feed rows to one pane in order and return the rows it printed."""
    out: list[dict] = []
    now = start
    for row in rows:
        out.extend(path.feed(row, now=now))
        now += 1.0
    return out


def _states(rows: Sequence[dict]) -> list[str]:
    return [str(row.get("to_state") or "") for row in rows]


# ── one grant, one pane ─────────────────────────────────────────────────────


def test_three_followers_of_one_session_draw_one_grant_and_one_end():
    """The defect: a pane following three projects drew the grant three times."""
    # Each stream carries the same grant and the same end, stamped apart.
    streams = {
        project: [
            _lift_row(project, observed=f"2026-10-14T18:29:4{index}Z"),
            _lift_row(project, ended="expired", observed=f"2026-10-14T18:40:0{index}Z"),
        ]
        for index, project in enumerate(PROJECTS)
    }
    followers = {project: _follower() for project in PROJECTS}
    # The pane draws the three streams interleaved in observed-time order, the
    # order the three producers wrote them, not one project's stream end to end.
    ordered = sorted(
        (row["observed_at"], project, row)
        for project, rows in streams.items()
        for row in rows
    )
    printed: list[dict] = []
    now = 1000.0
    for _, project, row in ordered:
        printed.extend(followers[project].feed(row, now=now))
        now += 1.0

    grants = [row for row in printed if row["to_state"] == lift_watch.LIFT_GRANTED]
    ends = [row for row in printed if row["to_state"] == lift_watch.LIFT_ENDED]
    assert len(grants) == 1, f"the grant drew {len(grants)} times on one pane"
    assert len(ends) == 1, f"the end drew {len(ends)} times on one pane"
    assert grants[0]["lift_id"] == LIFT_ID


def test_a_second_lift_id_draws_its_own_row():
    path = _follower()
    first = _feed(path, [_lift_row(PROJECTS[0], lift_id=LIFT_ID)])
    second = _feed(path, [_lift_row(PROJECTS[0], lift_id=SECOND_LIFT_ID)])
    assert _states(first) == [lift_watch.LIFT_GRANTED]
    assert _states(second) == [lift_watch.LIFT_GRANTED]
    assert second[0]["lift_id"] == SECOND_LIFT_ID


def test_a_follower_for_another_session_draws_the_grant_once():
    grant = _lift_row(PROJECTS[0])
    mine = _feed(_follower(session=SESSION), [grant])
    theirs = _feed(_follower(session=OTHER_SESSION), [grant])
    assert _states(mine) == [lift_watch.LIFT_GRANTED]
    assert _states(theirs) == [lift_watch.LIFT_GRANTED]


def test_the_lift_row_carries_a_structured_lift_id():
    row = _lift_row(PROJECTS[0])
    assert row["lift_id"] == LIFT_ID
    assert row["to_state"] == lift_watch.LIFT_GRANTED


# ── the claim's shape and lifecycle ─────────────────────────────────────────


def test_the_claim_is_an_exclusive_create_per_session_lift_and_transition():
    grant = lift_watch.LIFT_GRANTED
    end = lift_watch.LIFT_ENDED
    assert take_lift_claim(SESSION, LIFT_ID, grant) is True
    assert take_lift_claim(SESSION, LIFT_ID, grant) is False
    names = {path.name for path in lift_claim_dir(SESSION).iterdir()}
    assert names == {f"{LIFT_ID}__{grant}"}
    # The end transition is its own claim.
    assert take_lift_claim(SESSION, LIFT_ID, end) is True


def test_a_drawn_grant_leaves_a_claim_file_named_for_the_lift():
    path = _follower()
    _feed(path, [_lift_row(PROJECTS[0])])
    names = {entry.name for entry in lift_claim_dir(SESSION).iterdir()}
    assert names == {f"{LIFT_ID}__{lift_watch.LIFT_GRANTED}"}


def test_a_withheld_lift_row_takes_no_claim():
    """The claim is taken when the row prints, not when it is offered."""
    path = _follower()
    _feed(path, [_lift_row(PROJECTS[0])])
    before = {entry.name for entry in lift_claim_dir(SESSION).iterdir()}
    # A baseline for a group the pane already drew draws nothing.
    assert _feed(path, [_lift_row(PROJECTS[0], kind="baseline")]) == []
    after = {entry.name for entry in lift_claim_dir(SESSION).iterdir()}
    assert after == before


def test_the_end_prunes_every_claim_for_the_lift():
    path = _follower()
    _feed(path, [_lift_row(PROJECTS[0])])
    assert (lift_claim_dir(SESSION) / f"{LIFT_ID}__{lift_watch.LIFT_GRANTED}").exists()
    _feed(path, [_lift_row(PROJECTS[0], ended="expired")])
    remaining = [
        entry.name
        for entry in lift_claim_dir(SESSION).iterdir()
        if LIFT_ID in entry.name
    ]
    assert remaining == [], "the end left a standing claim for the lift"


def test_a_stale_claim_is_removed_on_the_next_print():
    first = _follower()
    _feed(first, [_lift_row(PROJECTS[0])])
    claim = lift_claim_dir(SESSION) / f"{LIFT_ID}__{lift_watch.LIFT_GRANTED}"
    assert claim.exists()
    aged = time.time() - (LIFT_CLAIM_MAX_AGE_SECONDS + 60.0)
    os.utime(claim, (aged, aged))
    # A peer follower would be blocked by a live claim; a stale one is cleared
    # and retaken, so the row prints.
    second = _follower()
    assert _states(_feed(second, [_lift_row(PROJECTS[0])])) == [lift_watch.LIFT_GRANTED]


def test_the_claim_is_not_carried_in_the_follower_checkpoint():
    """The claims are shared files; the per-follower memory keeps the group."""
    path = _follower()
    _feed(path, [_lift_row(PROJECTS[0])])
    assert path.lift_groups == frozenset({GROUP})
    assert all(LIFT_ID not in group for group in path.lift_groups)
    # The claim lives under the session's own watch directory, not the
    # follower's carried state.
    assert lift_claim_dir(SESSION).is_relative_to(runs.crew_home())
    assert "watch" in lift_claim_dir(SESSION).parts
