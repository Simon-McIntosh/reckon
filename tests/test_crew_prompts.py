"""The composed dispatch contract must key manifest timing to starting a
wait, not to finishing work.

Two runs were lost because a worker waited across a background command it
had started, and its process lifetime did not survive the wait: one hit the
harness's background-wait ceiling, the other simply ran out of turns with an
empty stderr. Both were consistent with the letter of the old contract,
which only said the manifest goes out BEFORE finishing — a worker still
waiting on a background suite has, by that reading, not finished. This
module asserts the composed prompt now states the worker's process ends
with its turn, that a backgrounded command is never waited across, and that
the manifest is written before any such wait begins (and updated after, if
a later turn arrives) — plus the matching recovery fact that a run ending
mid-wait is resumable.
"""

from __future__ import annotations

from reckon.crew.node import NEEDS_HELP_MARKER, TaskNode
from reckon.crew.prompts import compose_prompt

LIFETIME_STATEMENT = "Your process ends when this turn ends"
NEVER_WAIT_ACROSS = "never wait across a backgrounded command"
WRITE_BEFORE_WAITING = "Write your manifest with what you know now before starting one"
KEYED_TO_STARTING = "keyed to starting the wait, not to finishing the work"
RESUMABLE_MID_WAIT = "A run that ends mid-wait is resumable"
LEAVES_A_RECORD = "leave a record naming exactly what you were waiting for"

MANIFEST_DELIVERY_INSTRUCTION = (
    "Write your manifest to {manifest_path} BEFORE finishing, then reply "
    "with that path and a summary."
)


def _node(*, role: str = "implement") -> TaskNode:
    return TaskNode(
        id="lifetime-node",
        goal="check the composed contract states the worker's process ends with its turn",
        plan="plan-a",
        section="s7",
        role=role,
        done_when="the composed prompt keys the manifest to starting a wait",
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )


def _prompt(*, role: str = "implement") -> str:
    return compose_prompt(
        node=_node(role=role),
        project="proj",
        worktree="/repo/worktrees/lifetime-run",
        working_directory="/repo/worktrees/lifetime-run",
        manifest_path="/state/runs/lifetime-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line-wrapping so a phrase can be found
    regardless of where the composer happened to break the line."""
    return " ".join(text.split())


def _time_fence(prompt: str) -> str:
    """The FENCE — TIME section, where the lifetime rule is stated."""
    return _flat(prompt.split("FENCE — TIME", 1)[1].split("FENCE —", 1)[0])


# ── The lifetime rule is stated, in one paragraph, with its recovery fact ──


def test_prompt_states_the_process_ends_with_the_turn():
    prompt = _flat(_prompt())

    assert LIFETIME_STATEMENT in prompt


def test_prompt_states_a_backgrounded_command_is_not_waited_across():
    prompt = _flat(_prompt())

    assert NEVER_WAIT_ACROSS in prompt


def test_prompt_keys_the_manifest_to_starting_the_wait_not_finishing_work():
    prompt = _flat(_prompt())

    assert WRITE_BEFORE_WAITING in prompt
    assert KEYED_TO_STARTING in prompt


def test_prompt_names_the_mid_wait_recovery_in_the_same_paragraph_as_the_rule():
    time_fence = _time_fence(_prompt())

    assert LIFETIME_STATEMENT in time_fence
    assert NEVER_WAIT_ACROSS in time_fence
    assert WRITE_BEFORE_WAITING in time_fence
    assert RESUMABLE_MID_WAIT in time_fence
    assert LEAVES_A_RECORD in time_fence


# ── The rule applies to every role, not only ones that can run commands ────


def test_lifetime_rule_is_present_for_a_role_whose_sandbox_forbids_execution():
    prompt = _flat(_prompt(role="review"))

    assert LIFETIME_STATEMENT in prompt
    assert NEVER_WAIT_ACROSS in prompt
    assert RESUMABLE_MID_WAIT in prompt


def test_lifetime_rule_is_present_for_the_test_role_too():
    prompt = _flat(_prompt(role="test"))

    assert LIFETIME_STATEMENT in prompt
    assert NEVER_WAIT_ACROSS in prompt
    assert RESUMABLE_MID_WAIT in prompt


def test_lifetime_rule_is_present_for_the_implementing_role():
    prompt = _flat(_prompt(role="implement"))

    assert LIFETIME_STATEMENT in prompt
    assert NEVER_WAIT_ACROSS in prompt
    assert RESUMABLE_MID_WAIT in prompt


# ── This adds a rule; it must not disturb the instructions already there ───


def test_needs_help_instruction_survives_unchanged():
    prompt = _prompt()

    assert f"`{NEEDS_HELP_MARKER} <one line>`" in prompt
    assert "tried:         what you attempted and the observable result" in prompt
    assert "options:       two or three concrete paths you can see" in prompt
    assert "leaning:       which one, and why" in prompt
    assert "cost-if-wrong: what must be redone if the wrong path is taken" in prompt


def test_manifest_path_delivery_instruction_survives_unchanged():
    prompt = _prompt()

    expected = MANIFEST_DELIVERY_INSTRUCTION.format(
        manifest_path="/state/runs/lifetime-run/manifest.md"
    )

    assert expected in prompt
