"""The contract lint refuses placeholders and two-deliverable goals, not punctuation.

A goal joins two deliverables only when a separator opens a clause that carries
its own action verb; a semicolon or "then" between the parts of one deliverable
stays one node. That leading token is read whole, so a hyphenated compound
("verify-gate") is the noun the author wrote rather than the verb it begins
with. A placeholder survives only as an angle-bracket token outside a quoted
string or a code span, or as a whole goal that is nothing but a truly vague
word. The rows below pin both halves: accepted goals that must survive, and
refusals that name a real defect and must still fire.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from reckon import crew

_MANIFEST = str(Path(tempfile.gettempdir()) / "goal-lint-manifest.md")

# The goal-shaped properties this lint speaks to. Each row asserts about exactly
# these, so a measure or scope fault elsewhere in the fixture cannot stand in for
# a narrowing that did not happen.
_GOAL_PROPERTIES = ("single-goal", "fully-specified")


def _node(**overrides: Any) -> crew.TaskNode:
    """A well-formed node; each row spoils only the goal property it studies."""
    fields: dict[str, Any] = {
        "id": "goal-lint-node",
        "goal": "record the launch matrix for one backend",
        "plan": "reckon:a-refusal-is-about-the-work",
        "section": "s2",
        "done_when": "uv run pytest tests/test_backends.py reports 34 passed",
        "write_paths": ["reckon/crew/node.py"],
        "time_budget": "20m",
        "manifest_path": _MANIFEST,
        "spec_level": "guided",
    }
    fields.update(overrides)
    return crew.TaskNode(**fields)


def _verdict(row: dict[str, Any]):
    return crew.validate_node(
        _node(goal=row["goal"], **row.get("node", {})),
        budget_ceiling=row.get("budget_ceiling", ""),
        execution_capable=row.get("execution_capable"),
    )


# The refused done-when s22's window-plan-currency-audit carried under
# --role investigate. The role declares execution_capable false, so the measure
# it names still has to route to a capable role or an explicit override.
_WINDOW_AUDIT_MEASURE = (
    "Read only: change no file outside the reports directory, and run no "
    "test suite beyond targeted pytest selections needed to confirm a landed "
    "verdict."
)

# The runtime message s22's accepted-shared-path-is-not-a-conflict quoted in its
# done-when. Angle brackets inside a quoted string are the author quoting a
# literal, so the row is accepted rather than refused as an unresolved template.
_QUOTED_RUNTIME_MESSAGE = "cannot accept <path>: live run <peer> claims <claim>"

# The review's own refused done-when survives only as this fragment, quoted
# verbatim from the plan. "TODO" here names a marker to count, which is a
# measure, not an instruction to fill in.
_REVIEW_DONE_WHEN_FRAGMENT = "the counts of TODO-FIXME, xfail and skip markers"


ACCEPTED_GOALS = (
    {
        "what": "a single deliverable joined by a semicolon",
        "goal": "restore the separator; the lint refuses two deliverables",
    },
    {
        "what": "a single deliverable joined by 'and then'",
        "goal": "compare the base and then the head",
    },
    {
        "what": "a goal whose done-when quotes a runtime message with <path>",
        "goal": "accept a path a live peer also claims",
        "node": {
            "done_when": (
                f'the refusal quotes "{_QUOTED_RUNTIME_MESSAGE}" verbatim '
                "beside the accepted verdict, and pytest passes"
            )
        },
    },
    {
        "what": "a done-when carrying the review's marker-counting fragment",
        "goal": "count the annotation markers the review names",
        "node": {
            "done_when": (
                f"a table carries the sentence count and "
                f"{_REVIEW_DONE_WHEN_FRAGMENT}, and pytest passes"
            )
        },
    },
    {
        "what": "a hyphenated compound beginning a clause is not an action verb",
        "goal": "Narrow promotion's impl-move, outcome and verify-gate refusals to the run they judge",
    },
)


REFUSED_ROWS = (
    {
        "what": "the window-plan-currency-audit measure under role investigate",
        "goal": "survey the window plan's currency",
        "node": {
            "role": "investigate",
            "done_when": _WINDOW_AUDIT_MEASURE,
        },
        "execution_capable": False,
        "property": "fully-specified",
    },
    {
        "what": "the pace row's 90m budget beyond a 60m fence",
        "goal": "record the pace row in the window table",
        "node": {"time_budget": "90m"},
        "budget_ceiling": "60m",
        "property": "bounded",
    },
    {
        "what": "two deliverables joined by a conjunction",
        "goal": "write the census and update the plan",
        "property": "single-goal",
    },
    {
        "what": "a second clause opening on a bare action verb still splits",
        "goal": "narrow the goal lint; verify the node validator",
        "property": "single-goal",
    },
)


@pytest.mark.parametrize("row", ACCEPTED_GOALS, ids=lambda row: row["what"])
def test_an_accepted_goal_keeps_both_goal_properties(row: dict[str, Any]) -> None:
    verdict = _verdict(row)
    for prop in _GOAL_PROPERTIES:
        assert prop not in verdict.failed_properties, (
            row["what"],
            prop,
            verdict.findings,
        )


@pytest.mark.parametrize("row", REFUSED_ROWS, ids=lambda row: row["what"])
def test_a_named_defect_still_refuses_its_property(row: dict[str, Any]) -> None:
    verdict = _verdict(row)
    assert row["property"] in verdict.failed_properties, (
        row["what"],
        row["property"],
        verdict.findings,
    )


def test_a_placeholder_outside_a_quote_or_code_span_is_refused() -> None:
    """The token half of the placeholder rule, shown firing."""

    def refused(goal: str):
        return "fully-specified" in _verdict({"goal": goal}).failed_properties

    assert refused("write the census for <node>")
    assert refused("write <the-census> for this section")
    # Quoted and code-span placeholders are the author quoting a literal.
    assert not refused('write the census for "<node>"')
    assert not refused("write the census for `<node>`")
    # A goal that is nothing but a placeholder word is the tiniest gap of all.
    assert refused("TODO")
