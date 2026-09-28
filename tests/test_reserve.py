"""The bookend reserve: a fraction of every window withheld from implementation.

The measure these tests exist to demonstrate is a pair, not a single refusal. A
reserve that does nothing admits both an implementation dispatch and a review at
the same window state, so asserting the refusal alone proves nothing — the
duplicate-admission is the state a working reserve has to separate.

The reserve must bind at the start of the window and not only near its ceiling.
A rule that withheld the fraction once the window filled would pass every
assertion taken at a nearly-full window, so the pair is asserted at an empty
window first and repeated at a nearly-full window.

An unreadable window is not an empty one. A reading nobody could read must not
be folded to the one figure that admits everything, so the unreadable state
refuses the roles the reserve withholds from and admits the bookends, and the
refusal says the window was unreadable. The pair is asserted at the dispatch
entry point as well as in the arithmetic, because a reserve the dispatch path
never calls refuses nothing however correct its verdict is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import reserve
from tests.test_dispatch_records_the_pace_row import (
    _dispatch,
    _Host,
    _prescribed_node,
    _spent_lane,
)
from tests.test_dispatch_records_the_pace_row import (
    host as _host_fixture,
)

# pytest injects a fixture by the name a case's parameter asks for, so the
# harness's fixture is bound under that name rather than the alias it arrives by.
host = _host_fixture

# A window with the shipped ceiling and a configured reserve, as a resolved
# budget block would carry them.
WINDOW = {"utilisation_ceiling_pct": 100.0, "bookend_reserve_pct": 20.0}


def test_empty_window_refuses_implementation_and_admits_review() -> None:
    """The pair at the start of the window: the same state, opposite verdicts.

    A reserve that held only as the window filled would admit both here. It is
    the empty window that separates a real reserve from that rule.
    """
    implementation = reserve.admit(
        WINDOW, role="implement", utilisation_pct=0.0, claim_pct=100.0
    )
    review = reserve.admit(WINDOW, role="review", utilisation_pct=0.0, claim_pct=100.0)

    assert implementation["admitted"] is False
    assert review["admitted"] is True
    assert implementation["limit_pct"] == 80.0
    assert review["limit_pct"] == 100.0
    assert "20" in implementation["reason"]


def test_nearly_full_window_keeps_the_same_pair() -> None:
    """The pair again near the ceiling, so the reserve is not a fill-time rule."""
    implementation = reserve.admit(
        WINDOW, role="implement", utilisation_pct=79.0, claim_pct=5.0
    )
    review = reserve.admit(WINDOW, role="review", utilisation_pct=79.0, claim_pct=5.0)

    assert implementation["admitted"] is False
    assert review["admitted"] is True


def test_a_modest_implementation_claim_is_still_admitted() -> None:
    """The reserve is a fraction, not a wall against all implementation work."""
    verdict = reserve.admit(
        WINDOW, role="implement", utilisation_pct=0.0, claim_pct=10.0
    )
    assert verdict["admitted"] is True


def test_the_reserve_key_moves_the_refusal_boundary() -> None:
    """Changing the flight key moves where an implementation dispatch is refused.

    One claim is admitted at the shipped fraction and refused at a larger one,
    so the boundary is data rather than a constant in the code.
    """
    claim = 70.0
    small = reserve.admit(
        WINDOW, role="implement", utilisation_pct=0.0, claim_pct=claim
    )
    large_block = {**WINDOW, "bookend_reserve_pct": 50.0}
    large = reserve.admit(
        large_block, role="implement", utilisation_pct=0.0, claim_pct=claim
    )

    assert small["admitted"] is True
    assert large["admitted"] is False
    assert reserve.role_ceiling_pct(WINDOW, "implement") == 80.0
    assert reserve.role_ceiling_pct(large_block, "implement") == 50.0


@pytest.mark.parametrize("role", ["review", "verify"])
def test_verify_is_admitted_on_the_same_footing_as_review(role: str) -> None:
    """Both named bookend roles are exempt, asserted by identity of ceiling."""
    assert reserve.is_bookend(role) is True
    assert reserve.role_ceiling_pct(WINDOW, role) == reserve.role_ceiling_pct(
        WINDOW, "review"
    )
    verdict = reserve.admit(WINDOW, role=role, utilisation_pct=0.0, claim_pct=100.0)
    assert verdict["admitted"] is True


def test_the_reserve_holds_from_the_start_of_the_window() -> None:
    """The withheld fraction is gone before any work is dispatched.

    The implementation ceiling is one reserved fraction below the window
    ceiling at zero utilisation, and it does not move as the window fills — the
    reserve reads the role and the fraction, never the window state.
    """
    empty = reserve.role_ceiling_pct(WINDOW, "implement")
    nearly_full = reserve.role_ceiling_pct(WINDOW, "implement")
    assert empty == 80.0
    assert nearly_full == empty
    assert reserve.ceiling_pct(WINDOW) - empty == reserve.reserve_pct(WINDOW)


def test_an_unset_key_still_withholds_the_declared_floor() -> None:
    """A missing flight key is not a zero reserve.

    A default that resolved to nothing would silently disable the reserve in
    every layer that omits it, so the refusal is asserted with the key absent.
    """
    bare = {"utilisation_ceiling_pct": 100.0}
    assert reserve.reserve_pct(bare) == reserve.DEFAULT_RESERVE_PCT
    implementation = reserve.admit(
        bare, role="implement", utilisation_pct=0.0, claim_pct=85.0
    )
    review = reserve.admit(bare, role="review", utilisation_pct=0.0, claim_pct=85.0)
    assert implementation["admitted"] is False
    assert review["admitted"] is True


def test_an_unreadable_window_refuses_implementation_and_admits_review() -> None:
    """The pair with no reading at all: absent is not empty.

    A utilisation nobody could read must not be folded to zero, so the pair
    holds in one state with the refusal saying the window was unreadable —
    which is also what tells this refusal apart from a full window's.
    """
    implementation = reserve.admit(
        WINDOW, role="implement", utilisation_pct=None, claim_pct=0.0
    )
    review = reserve.admit(WINDOW, role="review", utilisation_pct=None, claim_pct=0.0)

    assert implementation["admitted"] is False
    assert review["admitted"] is True
    assert "unreadable" in implementation["reason"]
    assert "could not be read" in implementation["reason"]
    assert "implement" in implementation["reason"]
    assert "20" in implementation["reason"]
    assert implementation["utilisation_pct"] is None
    assert implementation["projected_pct"] is None


@pytest.mark.parametrize("role", ["review", "verify"])
def test_a_bookend_is_admitted_when_the_window_is_unreadable(role: str) -> None:
    """The reserve is never withheld from a bookend, readable or not.

    Refusing here would bar the reviews the reserve exists to protect.
    """
    verdict = reserve.admit(WINDOW, role=role, utilisation_pct=None, claim_pct=100.0)
    assert verdict["admitted"] is True
    assert "could not be read" in verdict["reason"]


# Every role the reserve composes a verdict for. A role interpolated behind one
# fixed article reads correctly for the roles that article happens to agree with
# and wrongly for the rest, so the pairing is asserted per role rather than once.
COMPOSED_ROLES = ("implement", "review", "verify", "investigate", "test")

# The letters an indefinite "a" cannot precede. "a implement dispatch is
# refused" is what a fixed article reads as for a role beginning with one.
VOWEL_INITIAL = "aeiou"


@pytest.mark.parametrize("role", COMPOSED_ROLES)
def test_no_refusal_puts_a_fixed_article_before_the_role(role: str) -> None:
    """Every role reads correctly in the texts the reserve composes for it.

    A spent window refuses a role outright, and a window nobody could read
    refuses every role the reserve withholds from; a bookend meets the second
    of those as an admission, so the two texts together cover every state the
    reserve puts a role into. Each names its role, and where the role begins
    with a vowel neither puts "a" in front of it — which is how "a implement
    dispatch is refused" was composed from a fixed article.
    """
    unreadable = reserve.admit(WINDOW, role=role, utilisation_pct=None)["reason"]
    spent = reserve.admit(WINDOW, role=role, utilisation_pct=99.0, claim_pct=100.0)[
        "reason"
    ]

    assert role in unreadable, unreadable
    for text in (unreadable, spent):
        if role[:1] in VOWEL_INITIAL:
            assert f"a {role}" not in text, text


# The dispatch boundary: a correct verdict refuses nothing if the dispatch path
# never reaches it. The pair is asserted through the dispatch entry point, on
# the harness that drives one — a temporary crew home, a real worktree, and the
# assertion that nothing landed outside it.


def _node(config_home: Path, name: str, role: str) -> crew.TaskNode:
    """The harness's prescribed node, carrying the role under test."""
    node = _prescribed_node(config_home, name)
    node.role = role
    return node


def test_dispatch_at_the_reserve_boundary_refuses_implementation_not_review(
    host: _Host,
) -> None:
    """The pair at the window state the reserve owns, through the entry point.

    The window is filled past the reserve boundary and left below the budget
    gate's own ceiling, which is the band where the two refusals separate: the
    implementation dispatch is refused against the fraction the window keeps
    for review and verify, and a review dispatch meeting the same reading is
    admitted. The admitted half is what shows the refusal is the reserve's
    rather than the window's fill.
    """
    _spent_lane(host, "lane-at-the-reserve-boundary", backend="alpha", utilisation=85.0)

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(
            host,
            "boundary-implement",
            _node(host.config_home, "boundary-implement", "implement"),
        )

    message = str(refusal.value)
    assert "the window keeps 20% for review and verify roles" in message, message
    assert "implementation" in message, message

    record = _dispatch(
        host,
        "boundary-review",
        _node(host.config_home, "boundary-review", "review"),
    )

    assert record["role"] == "review"
    assert record["pace"]["group"] == "sol", record["pace"]
    assert record["pace"]["clocks"]["five_hour"]["utilisation"] == pytest.approx(0.85)


def test_dispatch_at_an_unreadable_window_refuses_implementation(
    host: _Host,
) -> None:
    """No member has reported on this lane's window, so no reading exists.

    The implementation node is refused with the unreadable reading named, and
    the review node meets the same window state and is admitted — the second
    half is what shows the refusal was the reserve rather than every dispatch
    being refused while the window is unknown.
    """
    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(
            host,
            "unreadable-implement",
            _node(host.config_home, "unreadable-implement", "implement"),
        )

    message = str(refusal.value)
    assert "could not be read" in message, message
    assert "implement" in message, message
    assert "20" in message, message

    record = _dispatch(
        host,
        "unreadable-review",
        _node(host.config_home, "unreadable-review", "review"),
    )

    assert record["role"] == "review"
    assert record["pace"]["clocks"]["five_hour"]["utilisation"] is None, record["pace"]
    assert [pointer["run_id"] for pointer in crew.list_live()] == [record["run_id"]]
