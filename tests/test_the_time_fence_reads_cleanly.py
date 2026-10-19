"""The time fence reads as clean sentences in every form it is composed in.

The composer writes a period after the fence and then writes the next sentence.
That period belongs to the bare-duration form, which carries a token such as
``20m`` with no sentence of its own. The statement form ends with its own
sentence, so the period left behind turns its last words into ``and stop and
report..`` or ``past the deadline..``. The fence must end each sentence with
exactly one period in both forms, and the bare form must keep the period the
golden brief-prompt snapshot already reads.

These checks compose a real prompt through ``compose_prompt`` in the three
shapes a fence is written in — a launch instant with a continuation multiple, a
launch instant with none, and no launch instant at all — and inspect only the
time-fence block of each.
"""

from __future__ import annotations

import re

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

LAUNCH = "2026-10-01T18:32:03Z"
BUDGET = "20m"

TIME_FENCE_BLOCK = re.compile(r"FENCE — TIME\n(.*?)\n\n", re.DOTALL)


def _node() -> TaskNode:
    return TaskNode(
        id="reads-cleanly-node",
        goal="compose a prompt whose time fence reads cleanly",
        plan="plan-a",
        role="implement",
        done_when="the fence ends each sentence with one period",
        write_paths=["reckon/crew/prompts.py"],
        time_budget=BUDGET,
    )


def _compose(*, launch_instant: str = "", grace_multiple: float | None = None) -> str:
    return compose_prompt(
        node=_node(),
        project="proj",
        worktree="/repo/worktree",
        working_directory="/repo/worktree",
        manifest_path="/state/runs/reads-cleanly-node/manifest.md",
        time_budget=BUDGET,
        launch_instant=launch_instant,
        needs_help_after_failures=2,
        grace_multiple=grace_multiple,
    )


def _fence_block(prompt: str) -> str:
    match = TIME_FENCE_BLOCK.search(prompt)
    assert match is not None, "the composed prompt carries no time-fence block"
    return match.group(1)


# The three shapes a fence is composed in: a launch instant with the
# continuation multiple, a launch instant without one, and no launch instant.
STATEMENT_WITH_MULTIPLE = _compose(launch_instant=LAUNCH, grace_multiple=1.5)
STATEMENT_WITHOUT_MULTIPLE = _compose(launch_instant=LAUNCH)
BARE_DURATION = _compose()


def test_the_statement_form_with_a_multiple_reads_cleanly() -> None:
    assert ".." not in _fence_block(STATEMENT_WITH_MULTIPLE)


def test_the_statement_form_without_a_multiple_reads_cleanly() -> None:
    assert ".." not in _fence_block(STATEMENT_WITHOUT_MULTIPLE)


def test_the_bare_duration_form_reads_cleanly() -> None:
    assert ".." not in _fence_block(BARE_DURATION)


def test_the_bare_duration_form_keeps_its_period_before_the_next_sentence() -> None:
    assert "20m. Your process ends" in BARE_DURATION


def test_no_time_fence_block_carries_two_consecutive_periods() -> None:
    for prompt in (STATEMENT_WITH_MULTIPLE, STATEMENT_WITHOUT_MULTIPLE, BARE_DURATION):
        assert ".." not in _fence_block(prompt)
