"""Neither prompt site still teaches the verbatim first-line rule the gate dropped.

The control gate admits a delivered red log on the facts its run recorded: a
non-zero exit line and at least one failing test id the head arm does not fail.
It reads no wording. Two prompt sites nevertheless told the worker that its
log's first line had to repeat the declared mutation verbatim — a rule the gate
no longer applies — so a worker could satisfy the prompt and still deliver a
log the gate refuses. These cases enter through ``compose_prompt``, the surface
the worker actually receives, and assert that neither the declaration block nor
the manifest template's ``negative_control_log`` gloss asks for a verbatim
first line, and that both state the facts admission reads.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

BLOCK_HEADER = "CONTRACT — THE NEGATIVE CONTROL THIS NODE DECLARES"
# The template line boundary the block sits immediately before, so a case can
# read the block alone rather than the whole prompt.
MANIFEST_BOUNDARY = "MANIFEST (write exactly these keys"
TEST_PATH = "tests/test_guard.py"
DECLARATION = "removing the guard turns its case red"

# The rule the control gate applies, read from its reader in promotion.py: the
# control log is judged on the facts its run recorded and on no wording of it.
# Both sites must state the two facts, and the declaration must be shown as
# something the worker applies rather than a string a gate matches.
FACTS_RULE = (
    "a non-zero `EXIT=` line and at least one failing test id the head arm "
    "does not fail"
)
WORDING_REFUSED = "never on the log's wording"
DECLARATION_NOT_MATCHED = "not a string any gate matches"


def _prompt() -> str:
    return compose_prompt(
        node=TaskNode(
            id="prompt-teaches-node",
            goal="the prompt teaches the rule the control gate applies",
            plan="plan-a",
            section="guard",
            role="implement",
            done_when="the prompt states the facts the control gate reads",
            write_paths=[TEST_PATH],
            time_budget="20m",
            negative_control=DECLARATION,
        ),
        project="proj",
        worktree="/repo/worktrees/prompt-teaches-run",
        working_directory="/repo/worktrees/prompt-teaches-run",
        manifest_path="/state/runs/prompt-teaches-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _flat(text: str) -> str:
    """Collapse the composer's line wrapping so a phrase is found anywhere."""
    return " ".join(text.split())


def _declaration_block(prompt: str) -> str:
    """The declaration block alone, up to the manifest template beside it."""
    assert BLOCK_HEADER in prompt
    return prompt.split(BLOCK_HEADER, 1)[1].split(MANIFEST_BOUNDARY, 1)[0]


def _manifest_log_line(prompt: str) -> str:
    """The manifest gloss line the worker fills with the control log's path."""
    return next(
        line
        for line in prompt.splitlines()
        if line.strip().startswith("negative_control_log:")
    )


def test_the_declaration_block_states_the_facts_the_gate_reads():
    block = _flat(_declaration_block(_prompt()))

    assert FACTS_RULE in block
    assert WORDING_REFUSED in block


def test_the_manifest_log_line_states_the_facts_the_gate_reads():
    gloss = _flat(_manifest_log_line(_prompt()))

    assert FACTS_RULE in gloss
    assert WORDING_REFUSED in gloss


def test_the_declaration_block_no_longer_asks_for_a_verbatim_first_line():
    block = _flat(_declaration_block(_prompt()))

    assert "verbatim" not in block
    assert "first line" not in block


def test_the_manifest_log_line_no_longer_asks_for_a_verbatim_first_line():
    gloss = _flat(_manifest_log_line(_prompt()))

    assert "verbatim" not in gloss
    assert "first line" not in gloss


def test_the_declaration_is_still_shown_as_what_the_worker_applies():
    prompt = _prompt()
    block = _flat(_declaration_block(prompt))

    # The declaration still reaches the worker exactly once, and the block
    # names what it is for: the mutation the worker applies and a reader
    # compares the delivered log against, not a string any gate matches.
    assert DECLARATION in prompt
    assert prompt.count(DECLARATION) == 1
    assert DECLARATION_NOT_MATCHED in block
