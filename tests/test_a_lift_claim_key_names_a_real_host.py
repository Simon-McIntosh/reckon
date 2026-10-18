"""A pane's lift-claim key names a real host and the node it runs on.

A session host runs every follower of one pane, and it may follow more than one
crew session, so a pane's lift claim must be keyed on the host rather than on
the session. The first cut of that key decided a follower was hosted from
``RECKON_FOLLOWER_OWNER``, and named only a pid and a start tick: an unhosted
follower whose own reloader wrote ``RECKON_FOLLOWER_OWNER`` into its
replacement image presented that variable even without a host, and a pid is
meaningful only on the node that issued it, so a sweep reading the key could not
tell a host that died here from one alive on another node that shares the home.

The fix decides hosting from ``RECKON_SESSION_HOST_SETTLE``, which only
``SessionHost._spawn`` stamps and a reload carries through unchanged, and names
the node in the key as ``host-<node>-<pid>-<start>``. These cases drive the
follower's own claim key and its row path, and the pane's shared claim lives
under a temporary crew home that the fixture proves is left untouched.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from reckon.crew import lift_watch, runs, session_host
from reckon.crew_follow_commands import (
    _lift_claim_token,
    follower_row_path,
    lift_claim_dir,
    lift_claim_key,
)

HOST_SESSION = "s-power"
OTHER_SESSION = "s-ground"
PROJECT = "alpha"
GROUP = "codex-sub"
LIFT_ID = "codex-sub-1792002586-64d825"

# The owner pair a session host stamps on each child it spawns. The reloader
# writes the same variable into a replacement image whether or not a host ever
# armed the follower, which is what made its presence the wrong hosted signal.
HOST_OWNER = runs._format_follower_owner((2226630, "2026-10-18T06:40:00Z"))
NODE = _lift_claim_token(socket.gethostname())


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
    # A follower is unhosted unless a test says otherwise, so both the owner
    # stamp a reload leaves behind and the host's settle stamp are cleared.
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


# ── (a) an unhosted follower that has reloaded keeps the per-session key ─────


def test_a_reloaded_follower_without_a_host_keeps_the_per_session_key(
    monkeypatch: pytest.MonkeyPatch,
):
    """The defect: reloading stamps the owner variable, faking a host key."""
    # A reloaded image carries RECKON_FOLLOWER_OWNER even with no host, and no
    # settle var, because only a real host ever sets it.
    monkeypatch.setenv(runs._FOLLOWER_OWNER_ENV, HOST_OWNER)
    monkeypatch.delenv(session_host.SETTLE_ENV, raising=False)
    assert lift_claim_key(HOST_SESSION) == HOST_SESSION
    assert lift_claim_key(HOST_SESSION) != lift_claim_key(OTHER_SESSION), (
        "without a host the pane is the session, so two sessions differ"
    )
    drawn = _follower(HOST_SESSION).feed(_lift_row(), now=1000.0)
    assert _states(drawn) == [lift_watch.LIFT_GRANTED]


# ── (b) a hosted follower's key carries the node name ────────────────────────


def test_a_hosted_follower_key_names_its_node(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(runs._FOLLOWER_OWNER_ENV, HOST_OWNER)
    monkeypatch.setenv(session_host.SETTLE_ENV, "1")
    key = lift_claim_key(HOST_SESSION)
    assert key.startswith(f"host-{NODE}-"), (
        f"a hosted key must name the node it runs on, got {key!r}"
    )
    assert key == f"host-{NODE}-2226630-2026-10-18T06:40:00Z"
    # The followers of one host file under one pane key, whatever session each
    # follows; a different host serves a different pane.
    assert lift_claim_dir(HOST_SESSION) == lift_claim_dir(OTHER_SESSION)


# ── (c) section 51's two-session case still draws one grant and one end ──────


def test_a_host_following_two_sessions_draws_one_grant_and_one_end(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(runs._FOLLOWER_OWNER_ENV, HOST_OWNER)
    monkeypatch.setenv(session_host.SETTLE_ENV, "1")
    mine = _follower(HOST_SESSION)
    theirs = _follower(OTHER_SESSION)
    grant = _lift_row()
    end = _lift_row(ended="expired")
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
