"""The manifest template a worker reads states what makes a gate log evidence:
its first line names the revision it ran at, the tree, and the command, and the
command it records is the one that ran rather than a template.

Four reviewers found the same defect in four unrelated nodes on 2026-09-21. A
gate command was recorded as ``PYTHONPATH=<worktree>`` — a placeholder rather
than the path it ran in. A landed record cited a command that prints nothing for
the figure it asserted. A record's verification conditions omitted the element
its run actually used, so a reader following them measures differently. And two
gate logs named neither the revision nor the command they ran, so a later reader
can tie them to a tree only through a manifest that may be gone. None of the
four was a defect in the work; all four were records that claimed a result no
one can reproduce from what the record says.

The template is the contract a worker actually reads, so the convention belongs
there. The assertions enter through ``compose_prompt``, which is where that
contract is rendered, and cover four facts: the template says a gate log's first
line names the revision, the tree and the command; it says the recorded gate
command is the one that ran rather than a template; the gate-command gloss
carries no angle-bracket worktree placeholder, which is the negative half and is
the shape the first measured instance produced; and the first-line convention
for the negative-control log survives unchanged, so this change cannot loosen
what the earlier landing established.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

# The sentence the template must state, and the exact unit the declared
# mutation removes: a gate log's first line names the revision, the tree and
# the command.
GATE_LOG_FIRST_LINE_SENTENCE = (
    "A gate log's first line names the revision it ran at, the tree, and the command"
)

# The template states the recorded gate command is the one that ran, never a
# template placeholder.
RECORDED_COMMAND_RAN = "the gate command that actually ran"
TEMPLATE_COMMAND_REFUSED = "never a template command"

# The placeholder token the first measured instance emitted in place of the
# path it ran in. Its absence from the gate-command gloss is the negative half.
WORKTREE_PLACEHOLDER = "<worktree>"

# The convention the template already carried before this change: the red log's
# own first line repeats the declared mutation verbatim. It must survive.
NEGATIVE_CONTROL_FIRST_LINE_CONVENTION = (
    "The log's first line repeats the declared mutation verbatim, so a log "
    "that failed for any other reason is refused"
)


def _node() -> TaskNode:
    return TaskNode(
        id="gate-log-tree-node",
        goal="state in the manifest template what makes a gate log evidence",
        plan="plan-a",
        section="",
        role="implement",
        done_when="the emitted template states the gate log first-line convention",
        write_paths=["reckon/crew/prompts.py", "tests/test_gate_log_names_its_tree.py"],
        time_budget="20m",
    )


def _prompt() -> str:
    return compose_prompt(
        node=_node(),
        project="proj",
        worktree="/repo/worktrees/gate-log-run",
        working_directory="/repo/worktrees/gate-log-run",
        manifest_path="/state/runs/gate-log-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _manifest_gloss(prompt: str, key: str) -> str:
    """The manifest gloss line a worker reads for ``key``.

    Extracted from the composed prompt rather than from the module constant, so
    the assertions measure the contract as it is actually rendered.
    """
    prefix = f"{key}:"
    for line in prompt.splitlines():
        if line.startswith("  ") and line.startswith(f"  {prefix}"):
            return line.strip()
    raise AssertionError(f"the manifest template emits no {key!r} gloss line")


def test_template_states_a_gate_log_first_line_names_the_revision_tree_and_command():
    prompt = _prompt()
    gate_log_gloss = _manifest_gloss(prompt, "test_logs")

    assert GATE_LOG_FIRST_LINE_SENTENCE in " ".join(gate_log_gloss.split())


def test_template_states_the_recorded_gate_command_is_the_one_that_ran():
    prompt = _prompt()
    gate_command_gloss = _manifest_gloss(prompt, "tests")

    assert RECORDED_COMMAND_RAN in gate_command_gloss
    assert TEMPLATE_COMMAND_REFUSED in gate_command_gloss


def test_gate_command_gloss_carries_no_angle_bracket_worktree_placeholder():
    prompt = _prompt()

    assert WORKTREE_PLACEHOLDER not in _manifest_gloss(prompt, "tests")
    # The same hazard applies to the log gloss the worker fills beside it: the
    # placeholder the first measured instance emitted is not a template token.
    assert WORKTREE_PLACEHOLDER not in _manifest_gloss(prompt, "test_logs")


def test_negative_control_first_line_convention_is_unchanged():
    assert NEGATIVE_CONTROL_FIRST_LINE_CONVENTION in _prompt()
