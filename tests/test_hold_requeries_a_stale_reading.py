"""A hold re-queries a reading past its shelf life and holds on what comes back.

A hold that rests on a figure the reader cannot vouch for is indistinguishable
from one that holds for no reason, and the coordinator routes around either.
So the figure a hold rests on must be a position rather than a snapshot: the
hold path resolves the reading through the reader that owns that rule, a
reading past its configured shelf life is re-queried, and the hold carries the
figure the probe returned rather than the one that provoked the question.

Three properties are under test here, each driving the real hold path from a
receipt recorded on disk:

* a receipt past its shelf life is re-queried and the hold carries the probe's
  figure, never the receipt's;
* a re-query that cannot answer leaves the serving state ``unknown`` and
  unknown does not hold — a missed hold costs some quota, a false hold costs
  the lane;
* a figure the probe observed from a pool the held group does not declare never
  becomes that group's position, however fresh it is.

The shelf life is a flight key, so these cases resolve it from a temporary
configuration home rather than from a caller-passed literal: the home the
override displaces is given a shelf life wide enough to trust the receipt, and
is asserted byte-identical before and after, because an isolated read does not
prove an isolated write.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import budget
from reckon.crew import budget_group as bg
from reckon.crew import hold as hold_mod
from reckon.crew import staleness

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
# The configured shelf life the home under test declares, and a reading well
# past it: the five-hour clock this figure measures may have rolled over inside
# the interval, which is what disqualifies the figure as a position.
SHELF_LIFE_MINUTES = 60
STALE_AGE = timedelta(hours=2, minutes=24)
# The shelf life the displaced home declares. Wide enough that the same receipt
# would be trusted there, so a run that resolved the wrong home could neither
# re-query nor produce the figure the assertions require.
DISPLACED_SHELF_LIFE_MINUTES = 100_000

GROUP = "wallet"
MEMBER = "lane"
# The pool the held group does not declare: another account's surface, whose
# figure must not be adopted as this group's position.
OTHER_POOL = "codex-main"

RECEIPT_USED_PERCENT = 99.0
PROBE_USED_PERCENT = 97.0
PROBE_AGE = timedelta(seconds=30)
CEILING = 90.0


def _flight_yaml(shelf_life_minutes: float) -> str:
    """A minimal flight declaration whose relevant key is the shelf life."""
    return (
        f"default_backend: {MEMBER}\n"
        "backends:\n"
        f"  {MEMBER}:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    sandbox: worktree-full\n"
        f"    budget_group: {GROUP}\n"
        "budget:\n"
        f"  evidence_shelf_life_minutes: {shelf_life_minutes}\n"
    )


def _receipt_payload(*, used_percent: float, age: timedelta) -> dict[str, Any]:
    """One recorded receipt, in the shape the client's rollout receipts carry.

    The window is named by its length in minutes and the observation stamp is
    the run's own: a receipt carrying no observation time could not be aged at
    all, which is a different case from the one under test.
    """
    stamp = (NOW - age).isoformat()
    return {
        "observed_at": stamp,
        "quota_windows": [
            {"window_minutes": 300, "used_percent": used_percent, "observed_at": stamp}
        ],
    }


def _write_receipt(
    home: Path, member: str, *, used_percent: float, age: timedelta
) -> Path:
    """Record one receipt inside a configuration home."""
    path = home / "receipts" / f"{member}.json"
    path.write_text(
        json.dumps(_receipt_payload(used_percent=used_percent, age=age)),
        encoding="utf-8",
    )
    return path


def _receipt_figure(home: Path, member: str) -> budget.window_reading.WindowFigure:
    """The five-hour figure of a receipt on disk, read by the reader that owns it.

    The receipt is read through the code path that consumes recorded receipts,
    so the fixture cannot drift from the recorded shape, and the figure is
    required to be present: an unreadable receipt would otherwise let the
    assertions below pass on an empty reading.
    """
    payload = json.loads((home / "receipts" / f"{member}.json").read_text("utf-8"))
    reading = budget._receipt_reading(payload, moment=NOW)
    figure = reading.figure(budget.CLOCK_FIVE_HOUR)
    assert figure is not None, "the receipt fixture carried no five-hour window"
    return figure


def _position(
    home: Path, *, member: str, group: str, pool: str, serving_state: str = "stale"
) -> bg.GroupPosition:
    """A declared group's position, resting on the receipt recorded for it.

    The receipt names the figure and the moment it was observed; the pool it
    was observed from is a declaration about the account rather than a receipt
    field, so it is stated here instead of being inferred from a lane name.
    ``serving_state`` is the label the reading arrived with — a receipt old
    enough to be labelled stale is exactly the input this rule exists for.
    """
    figure = _receipt_figure(home, member)
    assert figure.utilisation * 100.0 == pytest.approx(RECEIPT_USED_PERCENT)
    reading: dict[str, Any] = {
        "used_percent": figure.utilisation * 100.0,
        # The stamp travels as the text a composed row carries, which is what
        # the position and the reader below both age the figure from.
        "observed_at": figure.observed_at.isoformat(),
        "source": pool,
        "serving_state": serving_state,
    }
    config = {"backends": {member: {"budget_group": group}}}
    return bg.group_position(group, config, {member: reading}, now=NOW)


def _probe_returning(
    *, used_percent: float, source: str, age: timedelta = PROBE_AGE
) -> tuple[staleness.Probe, list[int]]:
    """A lane probe that answers with one fresh figure, counting its calls."""
    calls: list[int] = []

    def probe() -> staleness.Reading:
        calls.append(1)
        return staleness.Reading(
            used_percent=used_percent,
            observed_at=NOW - age,
            source=source,
            serving_state="will_serve",
        )

    return probe, calls


def _probe_refusing() -> tuple[staleness.Probe, list[int]]:
    """A lane probe whose transport refuses, counting its calls."""
    calls: list[int] = []

    def probe() -> None:
        calls.append(1)
        raise RuntimeError("probe transport refused")

    return probe, calls


def _snapshot(root: Path) -> dict[str, str]:
    """Every file under ``root``, digested, for a byte-identity check."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, str]]:
    """A temporary config home under test, and the untouched one it displaces.

    The process resolves its configuration home from ``RECKON_HOME``, so the
    home under test is a temporary one carrying the stale receipt the hold
    path reads and the flight declaration its shelf life is read from. The home
    that override displaces is a temporary path too — a suite must never digest
    the operator's own home — given the same receipt and a shelf life wide
    enough to trust it, so a run resolving that home instead would neither
    re-query nor satisfy the assertions, and the byte-identity check below
    would have nothing to compare.
    """
    monkeypatch.delenv("RECKON_FLIGHT_CONFIG", raising=False)
    monkeypatch.delenv("RECKON_HOME", raising=False)
    displaced_home = tmp_path / "displaced"
    monkeypatch.setenv("HOME", str(displaced_home))
    displaced = displaced_home / ".config" / "reckon"
    (displaced / "receipts").mkdir(parents=True)
    (displaced / "flight.yaml").write_text(
        _flight_yaml(DISPLACED_SHELF_LIFE_MINUTES), encoding="utf-8"
    )
    _write_receipt(displaced, MEMBER, used_percent=RECEIPT_USED_PERCENT, age=STALE_AGE)
    before = _snapshot(displaced)

    home = tmp_path / "home"
    (home / "receipts").mkdir(parents=True)
    (home / "flight.yaml").write_text(
        _flight_yaml(SHELF_LIFE_MINUTES), encoding="utf-8"
    )
    _write_receipt(home, MEMBER, used_percent=RECEIPT_USED_PERCENT, age=STALE_AGE)
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home, displaced, before


# ── A receipt past its shelf life is re-queried before a hold rests on it ───


def test_a_hold_re_queries_a_stale_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold carries the probe's figure, never the stale receipt's."""
    home, displaced, before = _isolated_home(tmp_path, monkeypatch)
    assert before, "the displaced home holds a sentinel, or the check proves nothing"
    # The shelf life in force is the temporary home's, so the receipt below is
    # stale under the configuration the hold path actually resolves.
    assert staleness.configured_shelf_life_minutes() == SHELF_LIFE_MINUTES
    position = _position(home, member=MEMBER, group=GROUP, pool=GROUP)
    assert position.age_seconds is not None
    assert position.age_seconds > SHELF_LIFE_MINUTES * 60

    probe, calls = _probe_returning(used_percent=PROBE_USED_PERCENT, source=GROUP)
    hold = hold_mod.hold_from_position(position, probe=probe, ceiling=CEILING, now=NOW)

    assert hold is not None, "a stale receipt past the ceiling justified no hold"
    # The figure is the probe's and not the receipt's: a hold resting on the
    # receipt would carry RECEIPT_USED_PERCENT, describing a window that may
    # already have closed.
    assert hold.figure == pytest.approx(PROBE_USED_PERCENT)
    assert hold.figure != RECEIPT_USED_PERCENT
    assert hold.age_seconds == pytest.approx(PROBE_AGE.total_seconds(), abs=1.0)
    assert calls == [1], "a reading past its shelf life was never re-queried"
    assert hold.source == GROUP
    assert _snapshot(displaced) == before, "the hold path wrote into a config home"


# ── A re-query that cannot answer is unknown, and unknown does not hold ─────


def test_a_failed_re_query_yields_unknown_and_does_not_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asymmetry: a missed hold costs quota, a false hold costs the lane."""
    home, displaced, before = _isolated_home(tmp_path, monkeypatch)
    assert before, "the displaced home holds a sentinel, or the check proves nothing"
    position = _position(home, member=MEMBER, group=GROUP, pool=GROUP)

    refusing, _ = _probe_refusing()
    resolved = staleness.resolve_configured_reading(
        staleness.Reading(
            used_percent=RECEIPT_USED_PERCENT,
            observed_at=NOW - STALE_AGE,
            source=GROUP,
            serving_state="stale",
        ),
        probe=refusing,
        now=NOW,
    )
    # The state the hold refuses on is asserted directly, so the refusal below
    # cannot be passing for an unrelated reason.
    assert resolved.serving_state == staleness.SERVING_STATE_UNKNOWN
    assert resolved.used_percent is not None, "a failed re-query erased the figure"

    probe, calls = _probe_refusing()
    hold = hold_mod.hold_from_position(position, probe=probe, ceiling=CEILING, now=NOW)

    assert hold is None, "a failed re-query was read as a figure and held on"
    assert calls == [1], "a reading past its shelf life was never re-queried"
    assert _snapshot(displaced) == before, "the hold path wrote into a config home"


# ── A borrowed probe reading never becomes the group's position ─────────────


def test_a_borrowed_probe_reading_never_becomes_the_groups_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A figure the probe observed from another pool is refused, however fresh.

    The receipt is past its shelf life, so the lane's probe is asked, and it
    answers with a figure it observed from a pool this group does not declare.
    A fresh figure from an account this group does not draw on is not this
    group's position, so no hold is produced. The same answer observed by the
    group's own pool is held, so the pool is what decides rather than the
    freshness or the distance above the ceiling.
    """
    home, displaced, before = _isolated_home(tmp_path, monkeypatch)
    assert before, "the displaced home holds a sentinel, or the check proves nothing"
    position = _position(home, member=MEMBER, group=GROUP, pool=OTHER_POOL)

    borrowed, calls = _probe_returning(
        used_percent=PROBE_USED_PERCENT, source=OTHER_POOL
    )
    hold = hold_mod.hold_from_position(
        position, probe=borrowed, ceiling=CEILING, now=NOW
    )

    assert hold is None, "a figure another pool observed became this group's position"
    assert calls == [1], "a reading past its shelf life was never re-queried"

    owned, _ = _probe_returning(used_percent=PROBE_USED_PERCENT, source=GROUP)
    held = hold_mod.hold_from_position(position, probe=owned, ceiling=CEILING, now=NOW)
    assert held is not None, "a figure this group's own pool observed justified no hold"
    assert held.figure == pytest.approx(PROBE_USED_PERCENT)
    assert _snapshot(displaced) == before, "the hold path wrote into a config home"
