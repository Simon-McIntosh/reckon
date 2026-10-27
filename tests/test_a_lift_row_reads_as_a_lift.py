"""A lift row reads as a lift, and a grant that replaced another names both.

On 2026-10-22 the lead granted an uncapped codex lift and saw ``new → working``
then ``working → landed`` on one ``codex-sub`` row, and asked whether the lift
had ended. The second row was the end of the credits lift the grant replaced: a
lift row is keyed by its group and was drawn in run-state words, and the painter
dropped the form, scope and id the producer put in its clause.

The event pair below is the one the producers wrote at 19:11Z that day, taken
verbatim from the imas-ambix watch stream: a granted uncapped lift followed by
the end of the credits lift it replaced, on session ``s21-nova``. Feeding them
through the follower's own row path must render one row naming both — the
``uncapped`` grant and the id it ``replaces`` — and no row reading ``landed``.

The declared negative control is "today's painter fails the one-row case": the
same fixture run against the painter before this change renders ``… → landed``
and names no replacement, so the row-count assertion below fails.
"""

from __future__ import annotations

from collections.abc import Sequence

from reckon.crew.ticker import Ticker
from reckon.crew_follow_commands import follower_row_path

SESSION = "s21-nova"
GROUP = "codex-sub"
PROJECT = "imas-ambix"
CREDITS_ID = "codex-sub-1792567319-541307"
UNCAPPED_ID = "codex-sub-1792696292-a93427"
OBSERVED = "2026-10-22T19:11:33Z"

# A pane wide enough to show the whole naming clause, so the assertion reads the
# clause the painter composed rather than what a cut left of it.
_WIDE = 600


def _row(
    *,
    to_state: str,
    previous: str | None,
    detail: str,
    lift_id: str,
) -> dict:
    """One lift row in the producer's own shape, as the stream carried it."""
    return {
        "admitted_from": None,
        "alias": "",
        "backend": "",
        "blocked": 0,
        "classification": None,
        "detail": detail,
        "effort": "",
        "event": "transition",
        "from_state": previous,
        "lift_id": lift_id,
        "model": "",
        "node": GROUP,
        "observed_at": OBSERVED,
        "process_alive": None,
        "project": PROJECT,
        "recovery": "",
        "recovery_classification": to_state,
        "role": "",
        "run_id": None,
        "session": SESSION,
        "to_state": to_state,
        "unpromoted": 0,
        "working": 0,
    }


def _event_pair() -> list[dict]:
    """The pair the producers wrote: the grant first, then the end it replaced.

    ``lift_watch`` writes the newly-in-force grants before the ends of the lifts
    they replaced, which is the order the live stream carries.
    """
    return [
        _row(
            to_state="lift-granted",
            previous=None,
            detail=(
                "lift granted: group 'codex-sub', form uncapped, multiple None, "
                f"scope session:{SESSION}, id '{UNCAPPED_ID}'"
            ),
            lift_id=UNCAPPED_ID,
        ),
        _row(
            to_state="lift-ended",
            previous="lift-in-force",
            detail=(
                "lift ended: cause clear, group 'codex-sub', form credits, "
                f"multiple None, scope session:{SESSION}, id '{CREDITS_ID}'"
            ),
            lift_id=CREDITS_ID,
        ),
    ]


def _printed(events: Sequence[dict]) -> list[dict]:
    """The rows one follower's own row path prints, in order."""
    # A claim that always permits keeps this a test of the painter rather than
    # of a shared crew home; the once-per-pane claim has its own tests.
    path = follower_row_path(
        session=SESSION,
        lift_claim=lambda lift_id, transition: True,
    )
    out: list[dict] = []
    now = 1000.0
    for event in events:
        out.extend(path.feed(event, now=now))
        now += 1.0
    out.extend(path.flush(now=now + 3600.0))
    return out


def _rendered(rows: Sequence[dict]) -> list[str]:
    return [Ticker(width=_WIDE).render(row) for row in rows]


def test_the_uncapped_grant_that_replaced_the_credits_lift_is_one_row_naming_both():
    rendered = _rendered(_printed(_event_pair()))

    naming_both = [
        line
        for line in rendered
        if all(word in line for word in ("uncapped", "granted", "replaces"))
    ]
    assert len(naming_both) == 1, (
        "exactly one row names the uncapped grant and the lift it replaces: "
        f"{rendered!r}"
    )
    row = naming_both[0]
    assert UNCAPPED_ID in row, f"the row keys on the granted lift id: {row!r}"
    assert CREDITS_ID in row, f"the row names the replaced lift id: {row!r}"


def test_no_lift_row_reads_as_landed():
    rendered = _rendered(_printed(_event_pair()))
    landed = [line for line in rendered if "landed" in line]
    assert landed == [], f"a lift row still reads as a run that landed: {landed!r}"


def test_the_grant_row_names_the_lifts_form_and_scope():
    rendered = _rendered(_printed(_event_pair()))
    naming = [line for line in rendered if "uncapped" in line]
    assert naming, f"a lift row names its form: {rendered!r}"
    assert any(f"session:{SESSION}" in line for line in naming), (
        f"a lift row names its scope: {naming!r}"
    )
