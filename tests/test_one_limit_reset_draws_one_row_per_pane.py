"""One limit reset draws one row per pane, however many followers serve it.

The pane a session reads is served by more than one follower: a session host
runs one per project the session follows, a second follower streams the same
session read-only while the first holds the registration, and every one of them
reads the station's rows. A reset is a row about the account rather than about
a run, so each follower drew it, and a session read one announced reset as
several rows.

The pane's shared claim is what stops that: the followers of one pane write one
directory, and whichever of them reaches the row first draws it. The claim was
wired into the producer-stream row path and not into the station subscriber's
own path, which is the delivery path a follower takes whenever the node runs a
station — so the claim was absent exactly where several followers meet.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import station_store, ticker
from reckon.crew.station_subscriber import StationSubscriber
from reckon.crew_follow_commands import lift_claim_dir

PROJECT = "proj"
SESSION = "session-a"
GROUP = "claude-sub"
BASE = datetime(2026, 11, 15, 18, 30, tzinfo=UTC)


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


class _Clock:
    """A clock the case steps, so a held row is released without sleeping."""

    def __init__(self, moment: datetime = BASE) -> None:
        self.now = moment.timestamp()

    def __call__(self) -> float:
        return self.now

    def step(self, seconds: float) -> None:
        self.now += seconds


def _store(root: Path) -> station_store.StationStore:
    store = station_store.StationStore(root / "runtime")
    store.heartbeat()
    return store


def _pane(root: Path, clock: _Clock) -> StationSubscriber:
    """One follower serving this pane, on the station's own row path."""
    return StationSubscriber(
        station_store.StationStore(root / "runtime"),
        PROJECT,
        session=SESSION,
        clock=clock,
    )


def _write_reset(
    store: station_store.StationStore,
    *,
    moment: datetime = BASE,
    reset_id: str = "reset-claude-sub-20261115T183000Z",
    group: str = GROUP,
) -> None:
    """One announced reset, in the shape the producer's sweep writes it."""
    payload = {
        "project": PROJECT,
        "event": "transition",
        "run_id": None,
        "node": group,
        "session": "",
        "from_state": None,
        "to_state": ticker.LIMIT_RESET,
        "observed_at": moment.isoformat(),
        "lift_id": reset_id,
        "detail": (
            f"usage limit reset ({group}, seven_day) at "
            f"{moment.strftime('%H:%M')} UTC, announced"
        ),
    }
    store.write_event(
        audience=SESSION,
        at=moment,
        project=PROJECT,
        run="",
        kind="transition",
        from_state="",
        to_state=ticker.LIMIT_RESET,
        payload=payload,
    )


def _draw(pane: StationSubscriber, clock: _Clock) -> list[Mapping[str, object]]:
    """Everything this follower draws on one pass, holds released."""
    rows = list(pane.poll())
    clock.step(ticker.SETTLE_WINDOW + 1)
    rows += list(pane.flush())
    return rows


def _resets(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [row for row in rows if row.get("to_state") == ticker.LIMIT_RESET]


# ── one reset, one row, however many followers ──────────────────────────────


def test_three_followers_of_one_pane_draw_the_reset_once(tmp_path: Path) -> None:
    """The defect: the station path carried no claim, so each follower drew it."""
    store = _store(tmp_path)
    clock = _Clock()
    panes = [_pane(tmp_path, clock) for _ in range(3)]
    _write_reset(store)

    drawn = [row for pane in panes for row in _resets(_draw(pane, clock))]

    assert len(drawn) == 1, f"one reset drew {len(drawn)} rows"
    assert drawn[0]["lift_id"] == "reset-claude-sub-20261115T183000Z"


def test_the_follower_that_drew_it_then_reads_it_once(tmp_path: Path) -> None:
    """The claim is the pane's, so the winner's row is not re-offered either."""
    store = _store(tmp_path)
    clock = _Clock()
    first, second = _pane(tmp_path, clock), _pane(tmp_path, clock)
    _write_reset(store)

    first_draw = _resets(_draw(first, clock))
    second_draw = _resets(_draw(second, clock))
    first_again = [row for row in _draw(first, clock) if _resets([row])]

    assert len(first_draw) == 1, "the first follower drew the reset once"
    assert second_draw == [], "the second follower drew a row already drawn"
    assert first_again == [], "the pane drew the reset twice"


def test_two_different_resets_draw_two_rows(tmp_path: Path) -> None:
    """The control: the claim keys on the reset, so two resets are two rows."""
    store = _store(tmp_path)
    clock = _Clock()
    later = BASE + timedelta(hours=2)
    _write_reset(store)
    _write_reset(
        store,
        moment=later,
        reset_id="reset-claude-sub-20261115T203000Z",
    )

    drawn = [
        row
        for pane in (_pane(tmp_path, clock), _pane(tmp_path, clock))
        for row in _resets(_draw(pane, clock))
    ]

    assert len(drawn) == 2, "two resets drew two rows between the followers"
    assert {row["lift_id"] for row in drawn} == {
        "reset-claude-sub-20261115T183000Z",
        "reset-claude-sub-20261115T203000Z",
    }


def test_the_pane_holds_one_claim_naming_its_own_session(tmp_path: Path) -> None:
    """The marker the fix introduces: the claim the pane took, under its own key."""
    store = _store(tmp_path)
    clock = _Clock()
    _write_reset(store)

    assert _resets(_draw(_pane(tmp_path, clock), clock)), "nothing was drawn"
    claims = _listing(lift_claim_dir(SESSION))

    assert any(
        path.endswith("reset-claude-sub-20261115T183000Z__limit-reset")
        for path in claims
    ), claims
