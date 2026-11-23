"""The build skill queues the whole plan before it composes or answers a review.

The retired rule chose sections one at a time and queued them only after the
review round was answered. The queue is now the first act: the one-call
plan-width dispatch is named before any review command, and the sections queue
held while their reviews are answered as they land.
"""

from pathlib import Path

from tests.test_skill_contracts import normalized

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills/reckon-build/SKILL.md"
REFERENCE = ROOT / "skills/reckon-build/references/sprint-orchestration.md"
GUIDANCE = ROOT / "AGENTS.md"
FILES = (SKILL, REFERENCE, GUIDANCE)

PLAN_WIDTH = (
    "reckon crew dispatch --project <project> --plan <slug> --session <session>"
)
REVIEW_COMMAND = "reckon crew review --project"
CREW_SURFACE = "#### The crew surface"

# Every wording that carried the retired rule: choose sections by hand, hold the
# plan back until a review round is answered, or queue it after that round.
RETIRED = (
    "choose each section",
    "never load a plan wholesale",
    "rather than loading a plan wholesale",
    "queue the chosen work after the review round",
    "queue every implementable section of each plan whose review round is answered",
    "once an owned plan's review round is answered",
    "queue all of them only after the review round",
    "after the review round is answered",
)


def test_plan_width_dispatch_is_named_before_any_review_command() -> None:
    loop = normalized(SKILL.read_text()).split(CREW_SURFACE)[0]

    # Show the instrument sees both commands before asserting their order.
    assert PLAN_WIDTH in loop, "the plan-width dispatch command is not in the loop"
    assert REVIEW_COMMAND in loop, "no review command is in the build loop"

    assert loop.index(PLAN_WIDTH) < loop.index(REVIEW_COMMAND)


def test_the_queue_is_the_first_act_and_reviews_are_answered_after() -> None:
    text = normalized(SKILL.read_text())

    queue_heading = text.index("## Queue the whole plan")
    reviews_heading = text.index("## Answer reviews as they land")
    assert queue_heading < reviews_heading
    assert queue_heading < text.index(REVIEW_COMMAND)


def test_no_retired_rule_survives_in_the_skill_reference_or_guidance() -> None:
    for path in FILES:
        words = normalized(path.read_text()).lower()
        for phrase in RETIRED:
            assert phrase not in words, f"{path.name} still carries {phrase!r}"


def test_the_guidance_queues_before_answering_the_review() -> None:
    words = normalized(GUIDANCE.read_text()).lower()

    assert "as the first act before any review is composed or answered" in words
    assert "queue every plan you own" in words
    assert "queue every reviewed plan" not in words
