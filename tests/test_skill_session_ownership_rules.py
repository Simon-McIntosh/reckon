"""Guard the three session-ownership rules the build skill states.

One follower per session stopped before it is re-armed, no backgrounded dispatch
loop or wave script, and a search bounded at the producer: each is pinned by an
anchor phrase in the process reference and in the Claude Code harness reference.
RECKON_SKILLS_ROOT overrides the tree read, so a copy with one rule removed can
be shown to fail the guard.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(os.environ.get("RECKON_SKILLS_ROOT", Path(__file__).resolve().parents[1]))
BUILD = ROOT / "skills" / "reckon-build"
SPRINT = BUILD / "references" / "sprint-orchestration.md"
HARNESS = BUILD / "references" / "orchestrator-harness" / "claude-code.md"

# One anchor per rule in each reference, beside its motivating measurement.
RULES = (
    (
        "one follower per session, stopped before it is re-armed",
        "Re-arm is a stop and an arm",
        "A monitor expires; the follower inside it does not",
    ),
    (
        "no backgrounded dispatch loop or wave script",
        "No backgrounded dispatch loop or wave script",
        "No backgrounded dispatch loop or wave script",
    ),
    (
        "a search bounded at the producer, | head is not a bound",
        "`| head` is not a bound",
        "A search is bounded at the producer, and `| head` is not a bound",
    ),
)


@pytest.mark.parametrize(("rule", "sprint_anchor", "harness_anchor"), RULES)
def test_rule_is_asserted_in_both_references(rule, sprint_anchor, harness_anchor):
    assert sprint_anchor in " ".join(SPRINT.read_text().split())
    assert harness_anchor in " ".join(HARNESS.read_text().split())


def test_skill_entry_point_points_at_the_process_section():
    ship = (BUILD / "SKILL.md").read_text()
    assert "sprint-orchestration.md` §17" in " ".join(ship.split())
