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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

OBSERVED = "observed"
UNOBSERVED = "unobserved"


@dataclass(frozen=True)
class GroupPosition:
    """One declared wallet's position, taken from its freshest member reading.

    ``member`` and ``reading`` name the backend whose observation the position
    rests on, and that observation itself.  ``observed_at`` is the stamp exactly
    as the reading carried it; ``age_seconds`` is its age at the moment this
    position was computed.  Both are ``None`` when no member reading carries a
    stamp that can be aged — the state is then ``unobserved`` rather than a
    zero, because absence of an observation is not an observation of absence.
    """

    group: str
    members: tuple[str, ...]
    member: str | None
    reading: Mapping[str, Any] | None
    observed_at: str | None
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
    competes with a dated sibling for the group's position.
    """
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        try:
            return datetime.fromtimestamp(float(stamp), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return _aware(parsed)


def _aware(moment: datetime) -> datetime:
    """Return ``moment`` as UTC-aware, reading a naive value as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
