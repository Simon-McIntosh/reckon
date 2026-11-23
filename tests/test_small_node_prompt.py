"""The composed dispatch prompt states the small-node rule.

A small change was costing a feature's ceremony: a 20-minute edit ran through
ten nodes. The rule that makes it cheap has to reach the worker in the prompt
itself — the composed prompt embeds no protocol reference, so a discipline
carried only by a reference file reaches nobody. This module composes the
prompt for a small implement node with the existing builder and asserts the
three clauses by their own wording: the size test, what a small node writes,
and where a large data file belongs. It asserts them again for a brief-carrier
prompt, which has no plan section and so keeps its `landing:` line on the run's
own record. It also asserts that the evidence rule composed beside the
landing sentence asks for a figure only where the node's own done-when states
a measure, and for none where it states none. Finally it asserts that the rule sits beside the landing sentence rather than
somewhere else in the prompt, and that it is a pure insertion.
"""

from __future__ import annotations

from dataclasses import replace

from reckon.crew.node import TaskNode
from reckon.crew.prompts import (
    EVIDENCE_FIGURE_ANNOUNCED,
    EVIDENCE_FIGURE_WITHHELD,
    SMALL_NODE_RULE,
    compose_prompt,
)

# The three clauses, by their own wording. Kept as flattened substrings so a
# line break in the composer cannot hide a clause that no longer says what the
# rule requires.
SIZE_TEST = (
    "A node is small when its diff has at most 50 changed lines (added plus "
    "deleted), or when it changes no product source or tests and has at most "
    "300 changed lines"
)
WHAT_A_SMALL_NODE_WRITES = (
    "A small node writes one `landing:` line in its manifest, stating what "
    "changed and its measure's figure"
)
EVIDENCE_RULE = (
    "Keep evidence prose to the landing line, and write an evidence fragment "
    "only for a negative result; the landing fragment is always permitted at "
    "its scope path"
)
LARGE_DATA_FILE_BELONGS_IN_RUN_DIRECTORY = (
    "Any single data file above 300,000 bytes belongs in the run directory, "
    "not the repository"
)

# The landing sentence the rule sits beside: a landing-capable node is told the
# rule where it is told how to land, so the two are read together.
LANDING_SENTENCE = (
    "final commit; promotion lands the `landing:` line on your plan section."
)

# The sentence a brief carrier reads in place of the plan one: a brief names no
# plan section, so its landing line stays on the run's own record.
BRIEF_LANDING_SENTENCE = (
    "The `landing:` line lands on this run's own record, because a brief names "
    "no plan section for a promotion to land it on."
)

# A brief names no plan section but carries the same node, so the rule has to
# reach this carrier too: a small node dispatched as a brief owes exactly what a
# small plan node owes.
BRIEF_TEXT = "Make the small change, then record it for the next reader."


def _node(*, done_when: str = "") -> TaskNode:
    return TaskNode(
        id="small-node",
        goal="check the composed prompt states the small-node rule",
        plan="plan-a",
        section="s3",
        role="implement",
        done_when=done_when
        or "the composed prompt carries the small-node rule beside the landing sentence",
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )


def _prompt(*, done_when: str = "") -> str:
    return compose_prompt(
        node=_node(done_when=done_when),
        project="proj",
        worktree="/repo/worktrees/small-node-run",
        working_directory="/repo/worktrees/small-node-run",
        manifest_path="/state/runs/small-node-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _brief_prompt(*, done_when: str = "") -> str:
    # A brief alone names no plan; a brief beside a plan keeps the plan carrier.
    return compose_prompt(
        node=replace(_node(done_when=done_when), plan="", section=""),
        project="proj",
        worktree="/repo/worktrees/small-node-run",
        working_directory="/repo/worktrees/small-node-run",
        manifest_path="/state/runs/small-node-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        can_write_worktree=True,
        brief=BRIEF_TEXT,
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line-wrapping so a phrase can be found
    regardless of where the composer happened to break the line."""
    return " ".join(text.split())


# ── Each clause is stated by its own wording ────────────────────────────────


def test_prompt_states_the_size_test():
    assert SIZE_TEST in _flat(_prompt())


def test_prompt_states_what_a_small_node_writes():
    assert WHAT_A_SMALL_NODE_WRITES in _flat(_prompt())


def test_prompt_states_the_evidence_rule_and_its_figure_side():
    prompt = _flat(_prompt())

    assert EVIDENCE_RULE in prompt
    # The figure is asked for only where the done-when states a measure,
    # and this node's states none.
    assert _flat(EVIDENCE_FIGURE_WITHHELD) in prompt


def test_prompt_places_a_large_data_file_in_the_run_directory():
    assert LARGE_DATA_FILE_BELONGS_IN_RUN_DIRECTORY in _flat(_prompt())


# ── The evidence rule is composed from the node's own done-when ─────────────
#
# The figure clause is composed from the node's own done-when, rather than the
# worker applying a conditional to a node it cannot see from the inside. The two
# sides are asserted against each other, so neither case can pass by carrying a
# constant.


def test_the_evidence_rule_is_composed_from_the_nodes_own_done_when():
    named = _flat(
        _prompt(
            done_when=(
                "the composed prompt names a figure artifact and its wording "
                "assertion fails when the figure sentence is removed"
            )
        )
    )
    unnamed = _flat(
        _prompt(
            done_when="the composed prompt carries the rule beside the landing sentence"
        )
    )

    # The prose obligation holds either way; the node's own done-when decides
    # the figure, and the prompt states the side this node is on.
    assert EVIDENCE_RULE in named
    assert _flat(EVIDENCE_FIGURE_ANNOUNCED) in named
    assert _flat(EVIDENCE_FIGURE_WITHHELD) not in named
    assert _flat(EVIDENCE_FIGURE_WITHHELD) in unnamed
    assert _flat(EVIDENCE_FIGURE_ANNOUNCED) not in unnamed


def test_a_word_merely_containing_a_figure_word_does_not_count_as_naming_one():
    prompt = _flat(
        _prompt(done_when="rewrite the paragraph describing the landing rule")
    )

    assert _flat(EVIDENCE_FIGURE_WITHHELD) in prompt
    assert _flat(EVIDENCE_FIGURE_ANNOUNCED) not in prompt


# ── A brief carrier reads the same three rules ──────────────────────────────


def test_a_brief_run_prompt_states_the_three_rules():
    prompt = _flat(_brief_prompt())

    assert SIZE_TEST in prompt
    assert WHAT_A_SMALL_NODE_WRITES in prompt
    assert EVIDENCE_RULE in prompt
    assert LARGE_DATA_FILE_BELONGS_IN_RUN_DIRECTORY in prompt
    assert prompt.count(_flat(SMALL_NODE_RULE)) == 1


def test_a_brief_run_is_told_its_landing_line_stays_on_the_run_record():
    prompt = _flat(_brief_prompt())

    assert BRIEF_LANDING_SENTENCE in prompt
    # The clause that decides the figure is composed for a brief node too, from
    # the same done-when, so the withholding reaches this carrier as well.
    assert _flat(EVIDENCE_FIGURE_WITHHELD) in prompt


# ── The rule sits beside the landing sentence, appears once, and is pure ─────


def test_rule_sits_beside_the_landing_sentence():
    prompt = _prompt()

    assert LANDING_SENTENCE in prompt
    # The rule follows the landing sentence with nothing between them, so a
    # landing-capable worker reads the size rule in the landing clause.
    assert (LANDING_SENTENCE + "\n" + SMALL_NODE_RULE) in prompt


def test_rule_appears_exactly_once_in_the_composed_prompt():
    prompt = _prompt()

    assert prompt.count(SMALL_NODE_RULE) == 1


def test_rule_is_a_pure_insertion(monkeypatch):
    """Mask the rule out of the contract the composer reads and recompose: the
    live prompt must differ by exactly that block, so no other element of the
    composition changed."""
    import reckon.crew.prompts as prompts_mod

    after = _prompt()
    assert SMALL_NODE_RULE in after

    without_rule = prompts_mod.PLAN_LANDING_CONTRACT.replace(SMALL_NODE_RULE, "", 1)
    assert without_rule != prompts_mod.PLAN_LANDING_CONTRACT
    monkeypatch.setattr(prompts_mod, "PLAN_LANDING_CONTRACT", without_rule)
    before = _prompt()

    assert after.replace(SMALL_NODE_RULE, "", 1) == before
