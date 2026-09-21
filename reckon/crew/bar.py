"""Recommend a dispatch position for a node from a provider window's state.

A node carries an *open-endedness* score: how much of its own shape a worker
must invent rather than read. Open-ended work is the work worth spending a
finite metered window on; work that is already prescribed is executed as
written by the free lane, which is not finite. The choice is the score against
a bar, and the bar is not fixed -- it rises as the metered window fills, so the
same node is admitted early in a drained week and refused late in a spent one.

Three properties are load-bearing rather than stylistic.

*Four outcomes, not three.* A single ``send`` covering both lanes cannot say
"route this local", and an implementation built on it returned ``hold`` for a
maximally prescribed node at a full window while returning ``send`` for a
maximally open-ended one -- the policy exactly inverted, and asserted as
intended. The two lanes are opposite recommendations and are named separately;
``split`` and ``hold`` complete the vocabulary because a node near the bar is a
close call rather than a verdict, and a node too open-ended for the free lane
is better left undispatched than sent there to be done badly.

*Prescription is judged before the window.* A prescribed node is local at any
fill, a completely full window included, because that is what a free lane is
for. At a full window the bar sits at its ceiling above every prescribed score,
so a node judged by the bar alone falls through it into hold -- the inversion
that put prescribed work on hold in the first implementation.

*Advise, never route.* The result carries a recommendation and the position it
was drawn from -- never a lane, a backend or an endpoint. A score a hair under
the bar is a close call an orchestrator should see, not a verdict, so the
orchestrator may decide against the recommendation in either direction and the
result records that it was overridden rather than refusing it.
"""

from __future__ import annotations

from dataclasses import dataclass

# The four outcomes the bar can report. The two sends are the opposite lanes
# and are named separately because one verdict covering both cannot say "route
# this local"; SPLIT exists because a node near the bar is a close call an
# orchestrator should see; and HOLD is for a node too open-ended to be sent to
# the free lane at all.
SEND_METERED = "send-metered"
SEND_LOCAL = "send-local"
SPLIT = "split"
HOLD = "hold"
RECOMMENDATIONS = (SEND_METERED, SEND_LOCAL, SPLIT, HOLD)

# How near the bar counts as a close call. A node within this margin below the
# bar is not off the metered lane for a reason of substance -- it is close
# enough that the right move is usually to split it into smaller nodes, each of
# which clears the bar, and dispatch both now.
NEAR_BAR_BAND = 0.15

# The open-endedness at or below which a node counts as prescribed: its shape
# is essentially read off its own record (one artifact, the file and line, a
# numeric gate with its before value, a literal control mutation), so the free
# lane executes it as specified. Above it there is enough left to invent that
# the free lane is not guaranteed to produce it as the record states it, and
# the node is judged against the bar instead.
PRESCRIBED_MAX = 0.25


@dataclass(frozen=True)
class Recommendation:
    """A recommendation with the position it was drawn from.

    ``verdict`` is always what the bar itself returned, so an override never
    rewrites the bar's own reading; ``override`` records what the orchestrator
    decided instead, and ``effective`` is the recommendation in force. Keeping
    the two apart is what lets a later reader see that the bar was overridden
    rather than that it agreed.

    No field names a lane, a backend or an endpoint. The verdict is a position,
    and the caller that knows the fleet maps it to a lane.
    """

    verdict: str
    bar: float
    score: float
    window_fill: float
    margin: float
    override: str | None = None

    @property
    def overridden(self) -> bool:
        """Whether an orchestrator decided against the bar's recommendation."""
        return self.override is not None

    @property
    def effective(self) -> str:
        """The recommendation in force: the override if there is one."""
        return self.override if self.override is not None else self.verdict


def recommend(
    window_fill: float,
    score: float,
    *,
    override: str | None = None,
) -> Recommendation:
    """Return the bar's recommendation for a node at this window state.

    ``score`` is the node's open-endedness, supplied by the caller: zero is a
    node whose shape is entirely prescribed, one is a node that must invent its
    own. Higher scores clear the bar earlier, and as ``window_fill`` rises the
    bar rises with it, so one fixed score travels from an admitted metered send
    toward hold as the window fills.

    A prescribed node (``score`` at or below :data:`PRESCRIBED_MAX`) is
    :data:`SEND_LOCAL` at every fill, decided before the bar is consulted,
    because the free lane executes it as written and holding it would waste the
    lane the score exists to find. Otherwise the bar decides: clearing it with
    room left in the window is :data:`SEND_METERED`, a close call below it is
    :data:`SPLIT`, and below that -- or at a window with no room left -- the
    node is :data:`HOLD`.

    ``override`` lets an orchestrator decide against the bar. It must be one of
    :data:`RECOMMENDATIONS`, since a value outside them is not a decision about
    the bar but a caller error; a decision the bar did not recommend is
    recorded on the result, never refused. The bar's own ``verdict`` is left
    untouched so the disagreement stays visible.
    """
    if override is not None and override not in RECOMMENDATIONS:
        raise ValueError(f"override {override!r} is not one of {RECOMMENDATIONS}")

    fill = _window_unit(window_fill)
    bar = _bar_threshold(fill)
    margin = score - bar

    return Recommendation(
        verdict=_verdict(fill, score, bar, margin),
        bar=bar,
        score=score,
        window_fill=fill,
        margin=margin,
        override=override,
    )


def _verdict(fill: float, score: float, bar: float, margin: float) -> str:
    """Return the outcome for a node, prescribed work first.

    The order is the policy. A prescribed node is local at every fill, so the
    prescription test runs first: at a full window the bar sits at its ceiling
    above a prescribed node's score, and judging that node by the bar alone
    would return hold where the free lane is required.
    """
    if score <= PRESCRIBED_MAX:
        return SEND_LOCAL
    if fill < 1.0 and score >= bar:
        return SEND_METERED
    if -NEAR_BAR_BAND <= margin < 0.0:
        return SPLIT
    return HOLD


def _bar_threshold(fill: float) -> float:
    """Return the open-endedness a node must reach to clear the metered bar.

    Zero while the window is empty, one at its ceiling, rising continuously in
    between so there is no tier boundary at which one node routes and the next
    does not. The curve is flat near both ends and steepest in the middle: a
    nearly empty window admits almost anything, a nearly full one admits only
    the most open-ended work.
    """
    return fill * fill * (3.0 - 2.0 * fill)


def _window_unit(value: float) -> float:
    """Return ``value`` as a fraction of a window, clamped to its domain.

    A fill below an empty window or above a full one is not a window state;
    clamping keeps the bar finite and monotone rather than extrapolating a
    curve past the domain it was defined on.
    """
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
