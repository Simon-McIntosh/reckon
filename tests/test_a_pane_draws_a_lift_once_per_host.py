"""A pane draws a lift once, however many sessions its session host follows.

A session host runs every follower of one pane, and it may follow more than one
crew session — a coordinator dispatching under two session names, say. The lift
claim that keeps a grant from drawing twice was keyed on the session, so a host
following two sessions drew each transition once per session: the same
``codex-sub`` grant landed on the pane twice in the same second, once with this
session's counters and once with another's.

These drive the follower's own row path — the same object the follower feeds —
once per session a host follows, and count the rows the pane prints. A follower
is hosted exactly when ``RECKON_SESSION_HOST_SETTLE`` — the variable a real
session host stamps and a reload carries through — is present in its
environment, so that variable's presence is what selects the host key; a
follower without it keeps the per-session key, and a follower's own
``RECKON_FOLLOWER_OWNER`` stamp does not make it hosted. The pane's shared claim
lives under a temporary crew home, and the fixture asserts afterwards that the
real crew home is untouched.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from reckon.crew import lift_watch, runs, session_host
from reckon.crew_follow_commands import follower_row_path, lift_claim_dir

HOST_SESSION = "s-power"
OTHER_SESSION = "s-ground"
PROJECT = "alpha"
GROUP = "codex-sub"
LIFT_ID = "codex-sub-1792002586-64d825"

# The owner pair a session host stamps on each child it spawns.
HOST_OWNER = runs._format_follower_owner((2226630, "2026-10-18T06:40:00Z"))
OTHER_HOST_OWNER = runs._format_follower_owner((2226999, "2026-10-18T06:41:00Z"))


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
    # A follower is unhosted unless a test says otherwise, so the per-session
    # cases below key on the session regardless of the ambient environment. The
    # settle variable is the hosted signal, so it is cleared here alongside the
    # owner stamp a reload would leave behind.
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    monkeypatch.delenv(session_host.SETTLE_ENV, raising=False)
    yield tmp_path
    assert _listing(real_claims) == before, "a test wrote the real crew home"


def _lift_row(*, ended: str | None = None) -> dict:
    """One lift row in the producer's own shape, built by ``lift_watch``."""
    lift = {
        "group": GROUP,
        "form": "five_hour",
        "pace_multiple": 1.8,
        "scope": "global",
        "id": LIFT_ID,
    }
    return lift_watch._lift_row(PROJECT, lift, kind="transition", ended=ended)


def _follower(session: str):
    return follower_row_path(session=session)


def _states(rows: Sequence[dict]) -> list[str]:
    return [str(row.get("to_state") or "") for row in rows]


# ── one host, one pane ───────────────────────────────────────────────────────


def test_a_host_following_two_sessions_draws_one_grant_and_one_end(
    monkeypatch: pytest.MonkeyPatch,
):
    """The defect: a host following two sessions drew each transition twice."""
    monkeypatch.setenv(runs._FOLLOWER_OWNER_ENV, HOST_OWNER)
    monkeypatch.setenv(session_host.SETTLE_ENV, "1")
    mine = _follower(HOST_SESSION)
    theirs = _follower(OTHER_SESSION)
    grant = _lift_row()
    end = _lift_row(ended="expired")
    # The producers write both grants before either end, so the grants reach the
    # two followers before the lift ends, as they do on a live pane.
    printed: list[dict] = []
    printed += mine.feed(grant, now=1000.0)
    printed += theirs.feed(grant, now=1001.0)
    printed += mine.feed(end, now=1002.0)
    printed += theirs.feed(end, now=1003.0)

    grants = [row for row in printed if row["to_state"] == lift_watch.LIFT_GRANTED]
    ends = [row for row in printed if row["to_state"] == lift_watch.LIFT_ENDED]
    assert len(grants) == 1, f"the grant drew {len(grants)} times on one pane"
    assert len(ends) == 1, f"the end drew {len(ends)} times on one pane"
    assert grants[0]["lift_id"] == LIFT_ID


def test_the_claim_is_keyed_on_the_host_when_one_is_present(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(runs._FOLLOWER_OWNER_ENV, HOST_OWNER)
    monkeypatch.setenv(session_host.SETTLE_ENV, "1")
    shared = lift_claim_dir(HOST_SESSION)
    assert shared.is_relative_to(runs.crew_home())
    assert shared == lift_claim_dir(OTHER_SESSION), (
        "the followers of one host must file under one pane key"
    )
    monkeypatch.setenv(runs._FOLLOWER_OWNER_ENV, OTHER_HOST_OWNER)
    assert lift_claim_dir(HOST_SESSION) != shared, (
        "a different host serves a different pane"
    )


def test_an_unhosted_follower_keys_on_its_session(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    assert lift_claim_dir(HOST_SESSION) != lift_claim_dir(OTHER_SESSION), (
        "without a host the pane is the session, so two sessions differ"
    )
    # And a follower without a host still draws the lift once for its session.
    drawn = _follower(HOST_SESSION).feed(_lift_row(), now=1000.0)
    assert _states(drawn) == [lift_watch.LIFT_GRANTED]


def test_two_unhosted_followers_of_two_sessions_each_draw_their_grant(
    monkeypatch: pytest.MonkeyPatch,
):
    """Without a host each session is its own pane, so two sessions draw two rows."""
    monkeypatch.delenv(runs._FOLLOWER_OWNER_ENV, raising=False)
    grant = _lift_row()
    printed = _follower(HOST_SESSION).feed(grant, now=1000.0)
    printed += _follower(OTHER_SESSION).feed(grant, now=1001.0)
    assert _states(printed) == [lift_watch.LIFT_GRANTED, lift_watch.LIFT_GRANTED]
