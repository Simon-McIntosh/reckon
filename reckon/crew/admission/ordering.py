"""Order queued requests fairly and report their placement."""

from __future__ import annotations

from . import launching as _launching
from . import service as _service
from .shared import (
    HELD_PHASE,
    QUEUED_PHASE,
    Any,
    Callable,
    Mapping,
    Sequence,
    datetime,
    parse_utc,
    queue_order,
)


def _fair_use_weights_configured(config: Mapping[str, Any]) -> bool:
    from reckon import flight as flight_module

    return flight_module.fair_use_weights_configured(config)


def _fair_use_weights(
    pointers: Sequence[Mapping[str, Any]],
    *,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> dict[str, float] | None:
    """Each project's declared fair-use weight, or ``None`` when none is set.

    The weights live in flight configuration, so they are resolved through the
    same per-project config the pass reads for every other question. Only the
    weights each config actually declares are merged, first declaration winning,
    and the equal default is applied once at the end: merging completed weight
    maps instead would let whichever project came first in the queue fix every
    other project's weight at the default. ``None`` is what tells the pass to
    keep the plain admission order: a configuration that declares no weight
    keeps exactly the order it had.
    """
    from reckon import flight as flight_module

    projects: list[str] = []
    configs: dict[str, Mapping[str, Any]] = {}
    for pointer in pointers:
        project = str(pointer.get("project") or "")
        if project in configs:
            continue
        projects.append(project)
        configs[project] = (
            config_for(pointer)
            if config_for is not None
            else _launching._resolve_lane_config(pointer, select_local=False)
        )
    declared: dict[str, float] = {}
    for project in projects:
        for name, weight in flight_module.declared_project_weights(
            configs[project]
        ).items():
            declared.setdefault(name, weight)
    if not declared:
        return None
    return flight_module.project_weights(
        {flight_module.QUEUE_KEY: {flight_module.QUEUE_PROJECT_WEIGHTS_KEY: declared}},
        projects,
    )


def weighted_project_order(
    ordered: Sequence[Mapping[str, Any]],
    weights: Mapping[str, float],
) -> list[Mapping[str, Any]]:
    """Interleave a priority-ordered queue so each project's share tracks its weight.

    Weighted deficit round robin across the projects that still hold a request:
    every such project accrues its weight per round, the largest deficit is
    served next and pays the round's total, and its highest-ranked request —
    the one the priority order already placed first for that project — is
    taken. A project with nothing left accrues nothing, so a queue shallower
    than its share never holds a slot idle while another project has work.
    """
    pending: dict[str, list[Mapping[str, Any]]] = {}
    first_seen: dict[str, int] = {}
    for index, pointer in enumerate(ordered):
        project = str(pointer.get("project") or "")
        pending.setdefault(project, []).append(pointer)
        first_seen.setdefault(project, index)
    deficits: dict[str, float] = dict.fromkeys(pending, 0.0)
    servable = list(pending)
    result: list[Mapping[str, Any]] = []
    while servable:
        total = sum(weights.get(project, 1.0) for project in servable)
        for project in servable:
            deficits[project] += weights.get(project, 1.0)
        chosen = max(
            servable, key=lambda project: (deficits[project], -first_seen[project])
        )
        deficits[chosen] -= total
        result.append(pending[chosen].pop(0))
        if not pending[chosen]:
            servable.remove(chosen)
    return result


def _seat_share_turns(
    rows: Sequence[Mapping[str, Any]],
    weights: Mapping[str, float],
) -> list[Mapping[str, Any]]:
    """Interleave rows, seat by seat, so each role receives its share exactly.

    The share is a deficit round robin counted in seats rather than in rounds:
    after ``k`` seats a role has been owed ``k * share`` of them, and the next
    seat goes to whichever role has received fewest against that quota. Comparing
    against the quota rather than the running deficit is what makes a role's
    share what it receives rather than what it averages — with a third and two
    thirds, every third seat is the smaller role's, not one seat in four from a
    tie broken the same way each time. A tie goes to the role the priority order
    reached first, and a role whose requests are exhausted leaves the rest of the
    seats to the other.
    """
    pending: dict[str, list[Mapping[str, Any]]] = {}
    first_seen: dict[str, int] = {}
    for index, pointer in enumerate(rows):
        role = _role_of(pointer)
        pending.setdefault(role, []).append(pointer)
        first_seen.setdefault(role, index)
    served: dict[str, int] = dict.fromkeys(pending, 0)
    servable = list(pending)
    result: list[Mapping[str, Any]] = []
    while servable:
        seated = sum(served.values())
        chosen = max(
            servable,
            key=lambda role: (
                seated * weights.get(role, 1.0) - served[role],
                -first_seen[role],
            ),
        )
        served[chosen] += 1
        result.append(pending[chosen].pop(0))
        if not pending[chosen]:
            servable.remove(chosen)
    return result


# The smallest share of a pass's seats one role takes while the other role waits.
# Proportional-only shares let a deep backlog starve the other role: nine
# implementations and one review would give the review a tenth of the seats even
# though a delivered run waits on its review to land. Each waiting role is
# guaranteed this share, and the rest of the seats are split by backlog.
ROLE_SEAT_SHARE_FLOOR = 1 / 3

# A queued request that is not a review is an implementation.
IMPLEMENTATION_ROLE = "implementation"


def _review_role() -> str:
    """The reviewer role name, read from the module that owns the definition."""
    from reckon.crew.recovery_vocabulary import REVIEW_ROLE

    return REVIEW_ROLE


def _role_of(pointer: Mapping[str, Any]) -> str:
    """Whether a queued request is a review or an implementation.

    The review predicate is the one the recovery surfaces already own, imported
    here because the review-delivery module reads the run commands at import
    time.
    """
    from reckon.crew.recovery_review_delivery import _is_review_run

    return _review_role() if _is_review_run(pointer) else IMPLEMENTATION_ROLE


def _role_seat_weights(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Each waiting role's share of the seats, in proportion to its backlog.

    A role's share is its count over the slice's total, raised to
    :data:`ROLE_SEAT_SHARE_FLOOR` where its backlog would give it less, and the
    shares are rescaled so they still sum to one. A slice holding one role gives
    that role every seat.
    """
    counts: dict[str, int] = {}
    for pointer in rows:
        role = _role_of(pointer)
        counts[role] = counts.get(role, 0) + 1
    if len(counts) < 2:
        return dict.fromkeys(counts, 1.0)
    total = sum(counts.values())
    floored = {
        role: max(count / total, ROLE_SEAT_SHARE_FLOOR)
        for role, count in counts.items()
    }
    scale = sum(floored.values())
    return {role: share / scale for role, share in floored.items()}


def role_share_order(
    ordered: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Share each project's slice of the queue between reviews and implementations.

    The share is applied inside the project's own slice and never across the
    queue: every project keeps the place the priority order gave it, so the
    project fairness the weighted round robin protects is untouched, and this is
    not a second interleaver run over its result. Inside a slice each role keeps
    the relative order it was ranked in, so fair use between sessions still
    governs within each role, and the two roles then take turns by their share.

    A slice holding one role only keeps the order it had.
    """
    result = list(ordered)
    projects: list[str] = []
    for pointer in result:
        project = str(pointer.get("project") or "")
        if project not in projects:
            projects.append(project)
    for project in projects:
        positions = [
            index
            for index, pointer in enumerate(result)
            if str(pointer.get("project") or "") == project
        ]
        if len(positions) < 2:
            continue
        rows = [result[index] for index in positions]
        weights = _role_seat_weights(rows)
        if len(weights) < 2:
            continue
        shared = _seat_share_turns(rows, weights)
        for position, pointer in zip(positions, shared, strict=True):
            result[position] = pointer
    return result


def role_cadence(
    ordered: Sequence[Mapping[str, Any]],
    *,
    launched: Sequence[str],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    """Per role, what one pass launched and the age of the oldest left waiting.

    ``launched`` names the run ids the pass launched. Every other request in
    ``ordered`` was left waiting, and its age is read from its ``queued_at``. The
    two figures answer the question a coordinator reading a pass asks: is one
    role's backlog draining, or is the other taking the seats it needs.
    """
    cadence: dict[str, dict[str, Any]] = {
        _review_role(): {"launched": 0, "waiting": 0, "oldest_waiting_seconds": None},
        IMPLEMENTATION_ROLE: {
            "launched": 0,
            "waiting": 0,
            "oldest_waiting_seconds": None,
        },
    }
    launched_ids = set(launched)
    for pointer in ordered:
        row = cadence[_role_of(pointer)]
        if str(pointer.get("run_id") or "") in launched_ids:
            row["launched"] += 1
            continue
        row["waiting"] += 1
        queued_at = parse_utc(pointer.get("queued_at"))
        if queued_at is None:
            continue
        age = round(max(0.0, (now - queued_at).total_seconds()), 3)
        oldest = row["oldest_waiting_seconds"]
        if oldest is None or age > oldest:
            row["oldest_waiting_seconds"] = age
    return cadence


def priorities_first(
    order: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Hoist explicitly reprioritised requests ahead of the rest of the lane.

    ``crew queue reprioritise`` states that a higher override sorts ahead of
    the roadmap key, and a coordinator reaching for one is overriding the
    session and project ranking as well: a request raised to 100 while it waits
    behind another session's older work is still behind. The queue's ordering
    ranks within a session and then within a project, so the override is
    applied once more over the lane's whole order, highest first, and every
    other request keeps the order it was given.

    A delivered review keeps its place at the head: it is what lets a run that
    has already finished land, and it is dispatched ahead of implementation
    however long that has waited.

    Which requests are raised and which are reviews is read from the readers
    that already own those definitions (:func:`reckon.crew.queue_order._override`
    and the review-delivery module), so the lane's ordering and this hoist can
    never disagree about what an override or a review is.
    """
    from reckon.crew.recovery_review_delivery import _is_review_run

    raised = sorted(
        (
            pointer
            for pointer in order
            if queue_order._override(pointer) is not None
            and not _is_review_run(pointer)
        ),
        key=lambda pointer: -queue_order._override(pointer),
    )
    if not raised:
        return list(order)
    hoisted = {id(pointer) for pointer in raised}
    rest = [pointer for pointer in order if id(pointer) not in hoisted]
    lead = 0
    while lead < len(rest) and _is_review_run(rest[lead]):
        lead += 1
    return rest[:lead] + raised + rest[lead:]


def fair_share_admission_order(
    queued: Sequence[Mapping[str, Any]],
    live: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    """The order one admission pass will launch the queue in.

    The priority order (:func:`reckon.crew.queue_order.admission_order`) still
    ranks the requests, and the projects are interleaved across it by their
    configured fair-use weights. With no weight declared the priority order is
    returned unchanged, so an install that sets none keeps exactly the order it
    had.
    """
    order = queue_order.admission_order(
        queued,
        [pointer for pointer in live if pointer.get("phase") != HELD_PHASE],
        now=now,
    )
    order = role_share_order(order)
    weights = _fair_use_weights(queued, config_for=config_for)
    if weights is not None:
        order = weighted_project_order(order, weights)
    return priorities_first(order)


def awaits_a_lane_slot(pointer: Mapping[str, Any]) -> bool:
    """Whether this pointer is a request the pass has still to launch.

    The queued phase is written when a request is stored and advanced when the
    pass launches it, and the launch records ``launched_at`` on the pointer in
    its own write. A pointer therefore carries both for as long as the phase
    write has not followed: a run that has launched, and may have finished,
    still reads ``queued``. Counting it as queued hides the run's real duty
    behind a wait that is already over and publishes a queue place it no longer
    holds, so a pointer carrying ``launched_at`` is never read as queued
    whatever its phase says.
    """
    if str(pointer.get("phase") or "") != QUEUED_PHASE:
        return False
    return not (pointer.get("launched_at") or pointer.get("launch_recorded_at"))


def queued_placements(
    pointers: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    lane: str = "",
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return each queued request's place in the order the pass admits by.

    Every surface that reports where a queued run stands reads its place here
    rather than ordering the queue itself: a second ordering is how two
    surfaces come to disagree about which request goes first. The order is the
    admission pass's own, computed by
    :func:`reckon.crew.queue_order.admission_order` on the same snapshot the
    pass ranks, including the configured project weights, so a request's
    reported place is the place it will be launched at.

    The place is published as three figures, because they answer three questions.
    ``position`` is its place among every queued request on the lane, which is
    the figure a dispatch reports when a dispatch is queued. ``rank`` is its
    place among its own session's queued requests, which is what a coordinator
    reading its own queue wants. ``effective_rank`` is the weighted launch
    place when weights are configured, or its place within its project when
    they are absent. ``reason`` names the signal that placed it. ``lane`` narrows
    the ordering to one backend's
    queued requests, matching the narrowing the pass applies to its own queue.

    A queued request the snapshot does not carry reads no place, so a caller
    asks only about requests it already holds. A queued pointer without a
    readable ``queued_at`` also reads no place: the ordering cannot place it, so
    it is left out of the ordering rather than allowed to take the whole read
    down. Its run is still listed as queued by every surface that reads the
    snapshot itself; only its place is withheld, and every other pointer keeps
    the place the ordering gives it.
    """
    moment = now or _service._now()
    queued = [
        pointer
        for pointer in pointers
        if awaits_a_lane_slot(pointer)
        and (not lane or str(pointer.get("backend") or "") == lane)
        and parse_utc(pointer.get("queued_at")) is not None
    ]
    if not queued:
        return {}
    placements: dict[str, dict[str, Any]] = {}
    session_rank: dict[str, int] = {}
    project_rank: dict[str, int] = {}
    details = queue_order.admission_order_details(
        queued,
        [pointer for pointer in pointers if pointer.get("phase") != HELD_PHASE],
        now=moment,
    )
    reasons = {str(pointer.get("run_id") or ""): reason for pointer, reason in details}
    weights = _fair_use_weights(queued, config_for=config_for)
    order = [pointer for pointer, _reason in details]
    order = role_share_order(order)
    if weights is not None:
        order = weighted_project_order(order, weights)
    order = priorities_first(order)
    from reckon.crew.recovery_review_delivery import _is_review_run

    for position, pointer in enumerate(order, 1):
        session = str(pointer.get("session") or "")
        project = str(pointer.get("project") or "")
        session_rank[session] = session_rank.get(session, 0) + 1
        project_rank[project] = project_rank.get(project, 0) + 1
        reason = reasons[str(pointer.get("run_id") or "")]
        override = queue_order._override(pointer)
        if (
            override is not None
            and not _is_review_run(pointer)
            and not reason.startswith("priority override")
        ):
            reason = f"priority override {override}"
        if weights is not None:
            reason = f"weighted fair use ({project}: {weights[project]:g}); {reason}"
        placements[str(pointer.get("run_id") or "")] = {
            "position": position,
            "rank": session_rank[session],
            "effective_rank": position
            if weights is not None
            else project_rank[project],
            "reason": reason,
        }
    return placements
