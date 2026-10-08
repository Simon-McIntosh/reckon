"""Quota positions read once per declared wallet, never per lane.

A backend's ``budget_group`` slot names the account quota it draws on.  Lanes
carrying distinct lane names but one declared group hold one allowance between
them, so a pace target, a bar or a reserve is a property of the group: computed
per lane it would report each share as though it were the whole.  Grouping is
read from resolved flight config — never inferred from a lane's name, an
identical probe reading, or a matching reset time, each of which is evidence
only that things were observed the same way.

A group's position is its freshest member reading, carried together with that
member's observation age, because a position whose age is unknown is not a
figure a governor may hold on.  There is deliberately no per-backend position
entry point: the only function returning a position takes a declared group
identifier, so a caller cannot compute one lane's share of a shared wallet and
report it as the wallet's own.

The figures read the reports the crew's own reader publishes, in that reader's
vocabulary: the provider's period names, and each utilisation as a fraction of
its own window.  A wallet's pace, bar and reserve therefore come from the
clocks a stream actually reported, never from a key this module guesses at —
a reading carrying none yields no figure rather than a plausible one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._timestamps import parse_utc
from reckon.crew import bar as bar_module
from reckon.crew import budget_lift as budget_lift_module
from reckon.crew import pace as pace_module
from reckon.crew import reserve as reserve_module
from reckon.crew import window_reading

OBSERVED = "observed"
UNOBSERVED = "unobserved"

# The two metered clocks a wallet's figures read, under the period names the
# provider's own streams carry and the reader publishes. The five-hour clock is
# the window that fills, so the bar is drawn against it; the seven-day clock is
# the week the allowance divides, and its reset stamp places the wallet within
# that week. Each utilisation is a fraction of its own window, the unit the
# stream reports — never a percentage this module would have to scale.
FILL_CLOCK = "five_hour"
WEEK_CLOCK = "seven_day"


@dataclass(frozen=True)
class GroupPosition:
    """One declared wallet's position, taken from its freshest member reading.

    ``member`` and ``reading`` name the backend whose observation the position
    rests on, and that observation itself.  ``observed_at`` is the stamp exactly
    as the reading carried it, in whichever form the reading stored it — an
    ISO-8601 string or an epoch number — and ``age_seconds`` is its age at the
    moment this position was computed.  Both are ``None`` when no member reading carries a
    stamp that can be aged — the state is then ``unobserved`` rather than a
    zero, because absence of an observation is not an observation of absence.
    """

    group: str
    members: tuple[str, ...]
    member: str | None
    reading: Mapping[str, Any] | None
    observed_at: str | int | float | None
    age_seconds: float | None
    state: str

    def as_dict(self) -> dict[str, Any]:
        """Return the position as plain data for a view or a record."""
        payload: dict[str, Any] = {
            "group": self.group,
            "members": list(self.members),
            "member": self.member,
            "observed_at": self.observed_at,
            "age_seconds": self.age_seconds,
            "state": self.state,
        }
        if self.reading is not None:
            payload["reading"] = dict(self.reading)
        return payload


def declared_groups(config: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Map each declared budget group to its member backends in declaration order.

    Membership comes from the ``budget_group`` slot on each backend in resolved
    flight config, so the map restates the declaration and nothing else.  A
    backend whose slot is absent, blank or not a string declares no group and
    joins none.

    ``config`` may be the resolved flight config itself or its ``backends``
    mapping, since a caller holding one of the two should not have to reach for
    the other to ask which wallet a lane belongs to.
    """
    members_by_group: dict[str, list[str]] = {}
    for backend, group in _declared_group_by_backend(config).items():
        if group is not None:
            members_by_group.setdefault(group, []).append(backend)
    return members_by_group


def declared_group_for(config: Mapping[str, Any] | None, backend: str) -> str | None:
    """Return one backend's declared group from resolved flight config, or ``None``
    when it declares none.

    Membership is the same declaration :func:`declared_groups` restates: the
    backend's ``budget_group`` slot, read from whatever config the caller
    resolved, so a host, project or override value is honoured over a shipped
    one. A backend the config does not name, or names without a group, declares
    no group here.
    """
    name = str(backend or "").strip()
    if not name:
        return None
    return _declared_group_by_backend(config).get(name)


def ungrouped(config: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return the backends declaring no budget group, in declaration order.

    An undeclared backend stays ungrouped however alike its siblings read:
    sharing a window length or a reset time with a declared group is evidence
    about how the two were observed, never about whose quota they draw on.
    """
    return tuple(
        backend
        for backend, group in _declared_group_by_backend(config).items()
        if group is None
    )


def all_members_review_excluded(group: str, config: Mapping[str, Any] | None) -> bool:
    """Whether every member of one declared wallet is barred from review routing.

    The bookend reserve withholds a fraction of a wallet's window for the review
    and verify roles, from the start of the window. On a wallet whose every
    member the configuration removes from review routing, no review can ever run
    there, so the reserved fraction would be spent by nobody and would only keep
    the implementation work that wallet does serve below its own ceiling. The
    predicate names that wallet so the reserve can be withheld only where the
    roles it protects can run.

    Membership comes from the same declaration :func:`declared_groups` restates,
    and the exclusion is read from the configuration itself. An undeclared group
    is not all-excluded: it names no members to be sure about, and until the
    exclusion is certain the reserve stays withheld rather than lifted.
    """
    members = declared_groups(config).get(group)
    if not members:
        return False
    from reckon.crew import recovery

    excluded = recovery._review_excluded_backends(
        config if isinstance(config, Mapping) else {}
    )
    return all(member in excluded for member in members)


def reserve_block_for_group(
    block: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
    group: str | None,
) -> dict[str, Any]:
    """Return a budget block whose bookend reserve suits one declared wallet.

    A wallet whose every member is barred from review routing withholds no
    bookend reserve, so the block it is judged through carries a zeroed
    ``bookend_reserve_pct``; every other wallet, and every undeclared grouping,
    is judged through the block unchanged. The caller hands the result to the
    same reserve judge it would have handed the original block, so the boundary
    moves only for the wallets that cannot serve the reviews the reserve is for.
    """
    resolved = dict(block) if isinstance(block, Mapping) else {}
    if group is not None and all_members_review_excluded(str(group), config):
        resolved[reserve_module.RESERVE_KEY] = 0.0
    return resolved


def effective_block(
    config: Mapping[str, Any] | None,
    group: str,
    *,
    readings: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    session: str | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """The budget block a declared group's reserves are judged through.

    Two rules can zero a wallet's reserves, and they are composed here in one
    resolution rather than through two parallel paths a reader would have to
    reconcile. A lift in force releases all three reserves for its group,
    raising the multiple to the lifted figure; and a wallet whose every member
    is barred from review routing withholds no bookend reserve. The lift is
    applied first and the review-exclusion rule then applied to its result, so a
    wallet under both resolves through the same call, and a wallet under neither
    is returned with its configured reserve untouched.

    Expiry is read here rather than run anywhere: the block carries the lifted
    figure only while the lift is in force, judged from the record, the group's
    readings and the wall clock alone.
    """
    moment = _aware(now) if now is not None else datetime.now(UTC)
    lifted = budget_lift_module.effective_budget(
        config,
        group=str(group),
        readings=readings,
        now=moment,
        session=session,
        path=path,
    )
    return reserve_block_for_group(lifted, config, group)


def _lift_readings(
    reading: window_reading.WindowReading | None,
) -> list[Any] | None:
    """One window reading, in the shape the lift resolver reads, or ``None``.

    The resolver reads ``{clock: {utilisation, resets_at}}`` rows in observed
    order, and the group's freshest report already carries both clocks with
    those two figures. Handing the resolver the same report a figure was drawn
    from lets one reading both place the group in its week and decide whether a
    reset-anchored lift has ended, rather than a second reading taken for the
    lift and a third for the bar.
    """
    if reading is None:
        return None
    row: dict[str, Any] = {}
    for clock in (FILL_CLOCK, WEEK_CLOCK):
        figure = reading.figure(clock)
        if figure is None:
            continue
        row[clock] = {
            "utilisation": figure.utilisation,
            "resets_at": figure.resets_at,
        }
    return [row] if row else None


def group_position(
    group: str,
    config: Mapping[str, Any] | None,
    readings: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> GroupPosition:
    """Return the position of one declared group from its freshest member.

    ``readings`` maps a backend name to the reading last taken for it; a member
    that supplied no reading does not participate.  The position is the reading
    of the member carrying the newest observation stamp — the group's freshest
    member, not its first in declaration order — and its age is measured against
    ``now`` (default: the current instant).

    ``group`` must name a declared group from ``config``.  A lane name is not a
    group, so a per-lane position cannot be asked for through this function: an
    unknown identifier raises rather than returning one backend's share of a
    shared wallet as the wallet's figure.  Where no member reading can be aged,
    the position is declared ``unobserved`` with no member and no age.
    """
    groups = declared_groups(config)
    members = groups.get(group)
    if members is None:
        declared = ", ".join(sorted(groups)) or "none"
        raise ValueError(
            f"{group!r} is not a declared budget group (declared groups: {declared})"
        )
    moment = _aware(now) if now is not None else datetime.now(UTC)
    freshest: tuple[datetime, float, str, Mapping[str, Any]] | None = None
    for member in members:
        reading = readings.get(member)
        if not isinstance(reading, Mapping):
            continue
        observed = _observed_moment(reading.get("observed_at"))
        if observed is None:
            continue
        age = max(0.0, (moment - observed).total_seconds())
        if freshest is None or observed > freshest[0]:
            freshest = (observed, age, member, reading)
    if freshest is None:
        return GroupPosition(
            group=group,
            members=tuple(members),
            member=None,
            reading=None,
            observed_at=None,
            age_seconds=None,
            state=UNOBSERVED,
        )
    _, age, member, reading = freshest
    return GroupPosition(
        group=group,
        members=tuple(members),
        member=member,
        reading=dict(reading),
        observed_at=reading.get("observed_at"),
        age_seconds=age,
        state=OBSERVED,
    )


@dataclass(frozen=True)
class GroupFigures:
    """One declared wallet's pace target, bar and reserve.

    Each figure is a property of the wallet and is derived from the wallet's own
    freshest member reading, never from a lane's: a pace target, a bar or a
    reserve computed per lane would divide one allowance four ways and report
    each share as though it were the whole.  ``fill`` is the wallet's five-hour
    utilisation as a fraction, ``pace`` is the allowance the wallet may spend in
    its next window, ``bar`` is the open-endedness a node must reach at that fill.
    ``reserve_pct`` is the fraction of the wallet's window withheld from work the
    bookends maintain the fleet with; it holds from the start of the window and
    is therefore reported whatever the reading says. A wallet whose every member
    is barred from review routing withholds none, because the roles the reserve
    protects can never run there.
    """

    group: str
    members: tuple[str, ...]
    member: str | None
    state: str
    fill: float | None
    pace: Mapping[str, Any] | None
    bar: float | None
    reserve_pct: float

    def as_dict(self) -> dict[str, Any]:
        """Return the figures as plain data for a view or a record."""
        return {
            "group": self.group,
            "members": list(self.members),
            "member": self.member,
            "state": self.state,
            "fill": self.fill,
            "pace": None if self.pace is None else dict(self.pace),
            "bar": self.bar,
            "reserve_pct": self.reserve_pct,
        }


def group_figures(
    group: str,
    config: Mapping[str, Any] | None,
    windows: Mapping[str, Any] | None = None,
    *,
    block: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    session: str | None = None,
) -> GroupFigures:
    """Return one declared wallet's three pacing figures, once for the wallet.

    ``group`` must name a declared group, exactly as :func:`group_position`
    requires: a lane name is not a wallet, so a per-lane figure cannot be asked
    for here and an unknown identifier raises rather than answering with one
    lane's share of a shared allowance.

    ``windows`` maps a backend name to its window reading — an already read
    :class:`~reckon.crew.window_reading.WindowReading` or a stream source the
    reader can open — the same vocabulary :func:`reckon.budget.group_pace`
    consumes, so both surfaces read one shape rather than two.  The wallet
    reads its freshest member's report once and each figure comes from that
    report's own clocks: the fill from the five-hour utilisation, the allowance
    from the seven-day utilisation together with its reset, the bar from the
    fill.  Utilisations are fractions of their own window, the unit the stream
    reports.

    The figures are delegated to the module that owns each one: the allowance to
    :mod:`reckon.crew.pace`, the bar to :mod:`reckon.crew.bar` and the withheld
    fraction to :mod:`reckon.crew.reserve`.  A wallet whose reading carries no
    week clock that can be placed reports no pace, and a reading carrying no
    fill reports no bar — absent rather than zero, because a zero fill would
    read as an empty window and admit everything.
    """
    groups = declared_groups(config)
    members = groups.get(group)
    if members is None:
        declared = ", ".join(sorted(groups)) or "none"
        raise ValueError(
            f"{group!r} is not a declared budget group (declared groups: {declared})"
        )
    moment = _aware(now) if now is not None else datetime.now(UTC)
    supplied = windows if isinstance(windows, Mapping) else {}
    base_config = (
        {**(config or {}), "budget": block}
        if isinstance(block, Mapping)
        else config
    )

    freshest = _freshest_member(members, supplied, moment=moment)
    member = None if freshest is None else freshest[0]
    reading = None if freshest is None else freshest[1]
    fill = None if reading is None else reading.utilisation(FILL_CLOCK)
    week = None if reading is None else reading.figure(WEEK_CLOCK)
    elapsed = None if week is None else _week_placement(week.resets_at, moment)
    pace: Mapping[str, Any] | None = None
    if week is not None and elapsed is not None:
        pace = pace_module.allowance_for_group(
            pace_module.GroupReading(
                group=group, utilisation=week.utilisation, elapsed_hours=elapsed
            ),
            config=config,
        ).as_dict()

    return GroupFigures(
        group=group,
        members=tuple(members),
        member=member,
        state=UNOBSERVED if reading is None else OBSERVED,
        fill=fill,
        pace=pace,
        # The bar's threshold, which its own module draws from the fill alone:
        # the score is what a node clears it with, so any score returns it.
        bar=None if fill is None else bar_module.recommend(fill, 1.0).bar,
        reserve_pct=reserve_module.reserve_pct(
            effective_block(
                base_config,
                group,
                readings=_lift_readings(reading),
                now=moment,
                session=session,
            )
        ),
    )


def _freshest_member(
    members: Iterable[str],
    windows: Mapping[str, Any],
    *,
    moment: datetime,
) -> tuple[str, window_reading.WindowReading] | None:
    """Return the member carrying the newest window reading, and that reading.

    A wallet is read once, from its freshest member.  A member that supplied
    nothing readable does not compete, and neither does a reading that cannot
    be aged: an age-less figure cannot be told from a current one, so letting
    it speak for the wallet would report an observation that was never made.
    """
    freshest: tuple[datetime, str, window_reading.WindowReading] | None = None
    for member in members:
        source = windows.get(member)
        if source is None:
            continue
        reading = _reading_value(source, moment=moment)
        if not reading.known or reading.observed_at is None:
            continue
        if freshest is None or reading.observed_at > freshest[0]:
            freshest = (reading.observed_at, member, reading)
    if freshest is None:
        return None
    return freshest[1], freshest[2]


def _reading_value(source: object, *, moment: datetime) -> window_reading.WindowReading:
    """Resolve one member's reading, opening a caller's stream source if given.

    An already read :class:`~reckon.crew.window_reading.WindowReading` is taken
    as it stands; anything else is handed to the reader, which reports its own
    explicit unknown rather than raising when the source cannot be read.
    """
    if isinstance(source, window_reading.WindowReading):
        return source
    return window_reading.read_windows(source, now=moment)


def _week_placement(stamp: object, moment: datetime) -> float | None:
    """Hours the wallet stands into its week, or ``None`` if it cannot be placed."""
    reset = _observed_moment(stamp)
    if reset is None:
        return None
    remaining = (reset - moment).total_seconds() / 3600.0
    return max(0.0, pace_module.WEEK_HOURS - remaining)


def _declared_group_by_backend(
    config: Mapping[str, Any] | None,
) -> dict[str, str | None]:
    """Return each backend's declared group name, or ``None`` when it has none."""
    if not isinstance(config, Mapping):
        return {}
    declared: dict[str, str | None] = {}
    declared_layer = config.get("backends")
    entries = declared_layer if isinstance(declared_layer, Mapping) else config
    for raw_backend, raw_settings in entries.items():
        backend = str(raw_backend)
        settings = raw_settings if isinstance(raw_settings, Mapping) else {}
        value = settings.get("budget_group")
        declared[backend] = value.strip() if isinstance(value, str) else None
        if not declared[backend]:
            declared[backend] = None
    return declared


def _observed_moment(stamp: object) -> datetime | None:
    """Return an observation stamp as an aware instant, or ``None`` if undated.

    An epoch-seconds number and an ISO-8601 string are both accepted; a string
    carrying no zone is read as UTC.  Anything else — a missing stamp, an
    unparsable one, a bool — returns ``None`` so an undated reading never
    competes with a dated sibling for the group's position. The shared parser
    is tolerant of surrounding space and of a lowercase zone designator; this
    reader's recorded contract refuses both.
    """
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        return parse_utc(stamp)
    if not isinstance(stamp, str):
        return None
    if stamp != stamp.strip() or stamp.endswith("z"):
        return None
    return parse_utc(stamp)


def _aware(moment: datetime) -> datetime:
    """Return ``moment`` as UTC-aware, reading a naive value as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
