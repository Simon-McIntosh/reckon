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
from reckon.crew.prompts import (
    FALSIFIABLE_EVIDENCE_CONTRACT,
    PLAN_LANDING_CONTRACT,
    compose_prompt,
)

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


# ── The worktree-landing contract reaches a repo-writing role only ──────────

LANDING_HEADER = "CONTRACT — LANDING YOUR RECORD"
LAND_OWN_SECTION = "your landing record to your own section of the plan"
LAND_EVIDENCE_ANCHOR = "your evidence anchor to the cumulative evidence record"
LAND_IN_WORKTREE = "both live in this worktree"
LAND_IN_COMMIT = "both go into your final commit"
FIGURE_WHEN_SHOWN = (
    "Use a figure wherever a spatial, plotted or sequential relationship is clearer"
    " shown than described"
)
FIGURES_DIRECTORY = "docs/figures/<topic>/"
PROJECT_ABSOLUTE_SRC = "src /<project>/figures/"
NO_IMAGE_OF_A_TABLE = "never an image of what is naturally a table"
NO_META_EDIT = "Do not edit the plan-version or plan-modified meta lines"
META_EDIT_REASON = "every worker touching them makes every merge conflict there"


def _prompt_readonly(*, role: str = "review") -> str:
    """The dispatch shape of a read-only role: the process operates in its
    delivery directory and the repository at the worktree is read-only."""
    return compose_prompt(
        node=_node(role=role),
        project="proj",
        worktree="/repo/worktrees/lifetime-run",
        working_directory="/state/runs/lifetime-run",
        manifest_path="/state/runs/lifetime-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def test_repo_writing_role_is_told_to_land_both_records_in_the_tree():
    prompt = _flat(_prompt(role="implement"))

    assert LANDING_HEADER in prompt
    assert LAND_OWN_SECTION in prompt
    assert LAND_EVIDENCE_ANCHOR in prompt
    assert LAND_IN_WORKTREE in prompt
    assert LAND_IN_COMMIT in prompt


def test_landing_clause_names_the_figure_convention_and_src_form():
    prompt = _flat(_prompt(role="implement"))

    assert FIGURE_WHEN_SHOWN in prompt
    assert FIGURES_DIRECTORY in prompt
    assert PROJECT_ABSOLUTE_SRC in prompt
    assert NO_IMAGE_OF_A_TABLE in prompt


def test_landing_clause_forbids_editing_the_meta_lines_with_the_reason():
    prompt = _flat(_prompt(role="implement"))

    assert NO_META_EDIT in prompt
    assert META_EDIT_REASON in prompt


def test_a_readonly_role_without_a_repository_change_receives_no_landing_clause():
    for role in ("review", "investigate"):
        prompt = _prompt_readonly(role=role)
        assert PLAN_LANDING_CONTRACT not in prompt
        assert LANDING_HEADER not in prompt


def test_readonly_prompt_keeps_the_unconditional_contracts():
    prompt = _prompt_readonly(role="review")

    assert FALSIFIABLE_EVIDENCE_CONTRACT in prompt
    assert "CONTRACT — DURABLE WRITES" in prompt


def test_landing_clause_is_a_pure_removable_insertion(monkeypatch):
    """Mask the landing contract and recompose: the implement prompt must differ
    from the live one by exactly that block, nothing else in the composition."""
    import reckon.crew.prompts as prompts_mod

    after = _prompt(role="implement")
    assert after.count(PLAN_LANDING_CONTRACT) == 1

    monkeypatch.setattr(prompts_mod, "PLAN_LANDING_CONTRACT", "")
    before = _prompt(role="implement")

    assert after.replace(PLAN_LANDING_CONTRACT, "", 1) == before
