"""The composed dispatch prompt states the small-node rule.

A small change was costing a feature's ceremony: a 20-minute edit ran through
ten nodes. The rule that makes it cheap has to reach the worker in the prompt
itself — the composed prompt embeds no protocol reference, so a discipline
carried only by a reference file reaches nobody. This module composes the
prompt for a small implement node with the existing builder and asserts the
three clauses by their own wording: the size test, what a small node writes,
and where a large data file belongs. It also asserts that a node whose
done-when names a figure is told it may write the figure but not the evidence
prose, that the rule sits beside the landing sentence rather than somewhere
else in the prompt, and that it is a pure insertion.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import SMALL_NODE_RULE, compose_prompt

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
NO_PROSE_OR_FIGURE_UNLESS_NAMED = (
    "writes neither evidence prose nor a figure unless its done-when names "
    "that artifact; naming one does not permit the other"
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


def _flat(text: str) -> str:
    """Collapse the prompt's manual line-wrapping so a phrase can be found
    regardless of where the composer happened to break the line."""
    return " ".join(text.split())


# ── Each clause is stated by its own wording ────────────────────────────────


def test_prompt_states_the_size_test():
    assert SIZE_TEST in _flat(_prompt())


def test_prompt_states_what_a_small_node_writes():
    assert WHAT_A_SMALL_NODE_WRITES in _flat(_prompt())


def test_prompt_states_that_prose_and_figure_need_a_done_when_naming_them():
    prompt = _flat(_prompt())

    assert NO_PROSE_OR_FIGURE_UNLESS_NAMED in prompt
    # Naming one artifact does not permit the other: the figure clause carries
    # its own limit, stated as a separate clause from the prose it sits beside.
    assert "naming one does not permit the other" in prompt


def test_prompt_places_a_large_data_file_in_the_run_directory():
    assert LARGE_DATA_FILE_BELONGS_IN_RUN_DIRECTORY in _flat(_prompt())


# ── A done-when that names a figure permits the figure, not the prose ───────


def test_done_when_naming_a_figure_permits_the_figure_but_not_the_prose():
    prompt = _flat(
        _prompt(
            done_when=(
                "the composed prompt names a figure artifact and its wording "
                "assertion fails when the figure sentence is removed"
            )
        )
    )

    # The clause tells exactly this node what it may do: it may write the figure
    # its done-when names, and the same clause withholds the evidence prose.
    assert NO_PROSE_OR_FIGURE_UNLESS_NAMED in prompt
    assert "unless its done-when names that artifact" in prompt
    assert "does not permit the other" in prompt


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
