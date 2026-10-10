"""A pace hold names the instant its own burn comparison releases, not the reset.

The hold is ``burn > multiple``, and burn is the utilisation over the elapsed
fraction of the window. With no further spend the utilisation holds, so the hold
releases once the elapsed fraction reaches ``utilisation / multiple`` — the
window start plus that share of the window, usually hours away, and not the
window reset the reason used to print days out. The figures below are built from
one fixture and derived at assertion time, so nothing here encodes what the
arithmetic happened to be on the day it was written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import budget

# The fixture: a 168 h window read 22.0 h in, at 21% used, against the 1.1x
# configured pace multiple. The burn is 0.21 / (22/168) = 1.60x, so the group is
# over pace and the hold applies to a non-bookend role.
WEEK_HOURS = 168.0
WEEK_MINUTES = 10_080
MULTIPLE = 1.1
UTILISATION = 0.21
ELAPSED_HOURS = 22.0
NOW = datetime(2026, 10, 8, 13, 0, 0, tzinfo=UTC)


def _window() -> tuple[datetime, datetime]:
    """The fixture window: its start, 22.0 h before ``NOW``, and its reset."""
    start = NOW - timedelta(hours=ELAPSED_HOURS)
    return start, start + timedelta(hours=WEEK_HOURS)


def _elapsed_fraction(moment: datetime) -> float:
    start, _reset = _window()
    return (moment - start).total_seconds() / (WEEK_HOURS * 3600.0)


def _release_at(*, factor: float = 1.0) -> datetime:
    """The instant the hold releases, derived from the fixture at assertion time.

    The reported burn is divided by the banked-reset factor, so the release
    instant divides by the same factor: a banked reset adds a whole extra window
    of budget, which lowers the burn and releases the hold sooner.
    """
    start, _reset = _window()
    share = UTILISATION / (MULTIPLE * factor)
    return start + timedelta(hours=share * WEEK_HOURS)


def _entry(moment: datetime, *, factor: float = 1.0) -> dict:
    """A group entry as ``group_pace`` emits it, read at ``moment``.

    The burn is the raw utilisation over the elapsed fraction at ``moment``,
    passed through ``budget._scale_for_banked_reset`` so the entry paces by the
    number the derivation itself emits, and the factor the release instant
    divides by is read from where that function records it, not kept here.
    """
    start, reset = _window()
    elapsed_fraction = _elapsed_fraction(moment)
    allowance: dict = {
        "group": "codex-sub",
        "utilisation": UTILISATION,
        "derived": min(1.0, elapsed_fraction * MULTIPLE),
        "pace_multiple": MULTIPLE,
        "burn_multiple": UTILISATION / elapsed_fraction,
        "window_minutes": WEEK_MINUTES,
        "resets_at": budget._iso(reset),
        "elapsed_hours": (moment - start).total_seconds() / 3600.0,
        "elapsed_fraction": elapsed_fraction,
    }
    if factor != 1.0:
        allowance = budget._scale_for_banked_reset(allowance, credit=factor - 1.0)
    return {"group": "codex-sub", "allowance": allowance}


def _parsed(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def test_a_held_dispatch_names_the_instant_its_own_burn_releases() -> None:
    """The hold reports the release instant, not the window reset days away."""
    verdict = budget.pace_hold(_entry(NOW), "implement")

    assert verdict["held"] is True
    assert verdict["releases_at"] is not None
    releases_at = _parsed(verdict["releases_at"])
    # Equal to the window start plus utilisation / multiple of the window,
    # within the second the ISO stamp is truncated to.
    assert abs((releases_at - _release_at()).total_seconds()) < 1.0
    # The release instant is sooner than the reset it used to report.
    assert releases_at < _parsed(verdict["resets_at"])
    assert verdict["releases_at"] in verdict["reason"]
    assert "until the window resets" not in verdict["reason"]


@pytest.mark.parametrize("factor", [1.0, 2.0])
def test_the_release_instant_is_pinned_by_the_holds_own_burn(factor: float) -> None:
    """One minute either side of the release, the hold flips as it says it will.

    The fixture is rebuilt at each moment so the reported burn tracks the
    storage, and the release it names is exactly where the hold admits.
    """
    release = _release_at(factor=factor)
    before = budget.pace_hold(
        _entry(release - timedelta(minutes=1), factor=factor), "implement"
    )
    after = budget.pace_hold(
        _entry(release + timedelta(minutes=1), factor=factor), "implement"
    )

    assert before["held"] is True
    assert after["held"] is False
    assert after["releases_at"] is None
    assert abs((_parsed(before["releases_at"]) - release).total_seconds()) < 1.0


def test_the_release_accounts_for_a_banked_reset() -> None:
    """A banked reset releases the hold sooner; a held reading says so.

    At a moment before the unscaled release but after the scaled one, the
    reading is held under the banked-reset scaling and would still be held
    without it — which is what makes the scaling load-bearing for the instant.
    """
    scaled = _release_at(factor=2.0)
    moment = scaled - timedelta(minutes=1)

    banked = budget.pace_hold(_entry(moment, factor=2.0), "implement")
    unscaled = budget.pace_hold(_entry(moment, factor=1.0), "implement")

    assert banked["held"] is True
    assert unscaled["held"] is True
    assert abs((_parsed(banked["releases_at"]) - scaled).total_seconds()) < 1.0


def test_the_release_reads_the_recorded_banked_reset_factor() -> None:
    """The instant divides by the factor ``_scale_for_banked_reset`` recorded.

    A fractional credit gives a factor that is not the whole extra window a
    reader might restate, so an implementation pinned to a private 2.0 would name
    the wrong instant here. The instant is instead pinned to the figure the
    scaling function itself recorded on the allowance.
    """
    factor = 1.5
    moment = _release_at(factor=factor) - timedelta(minutes=1)
    entry = _entry(moment, factor=factor)
    recorded = entry["allowance"]["reset_factor"]
    assert recorded == pytest.approx(factor)

    verdict = budget.pace_hold(entry, "implement")

    assert verdict["held"] is True
    assert (
        abs((_parsed(verdict["releases_at"]) - _release_at(factor=factor)).total_seconds())
        < 1.0
    )


def test_an_unplaceable_window_yields_no_release_instant() -> None:
    """An allowance whose window cannot be placed names no release instant.

    The hold still applies -- the burn comparison needs no reset -- but the
    instant it would release at cannot be derived without the window's own
    length and reset, so it is an honest absence rather than a fabricated time.
    """
    entry = _entry(NOW)
    entry["allowance"].pop("resets_at")

    verdict = budget.pace_hold(entry, "implement")

    assert verdict["held"] is True
    assert verdict["releases_at"] is None


def test_an_admitted_or_bookend_verdict_carries_no_release_instant() -> None:
    """Only a hold names a release instant; an admit and a bookend do not."""
    assert budget.pace_hold(_entry(NOW), "implement")["held"] is True

    bookend = budget.pace_hold(_entry(NOW), "review")
    assert bookend["bookend"] is True
    assert bookend["held"] is False
    assert bookend["releases_at"] is None

    admitted = budget.pace_hold(_entry(_release_at() + timedelta(hours=1)), "implement")
    assert admitted["held"] is False
    assert admitted["releases_at"] is None
