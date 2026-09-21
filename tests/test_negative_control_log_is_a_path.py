"""The manifest template a worker reads states that ``negative_control_log``
is the path alone, and gives its commentary a field of its own.

Four workers wrote ``negative_control_log`` four ways — a path plus an em dash
and a sentence, a sub-key mapping, a markdown heading — none matching another
and all refused at promotion. The value the guard resolves is a bare path, but
the gloss offered it as a long description inside the placeholder, so a worker
filling it naturally emitted the path and then the description. The template is the
contract a worker actually reads, so the shape belongs there: the value is the
path alone, with no other text on the line, and anything worth saying about the
log goes on a ``negative_control_note`` line beside it.

The assertions enter through ``compose_prompt``, which is where a worker's
contract is rendered, and cover three facts: the template says the value is the
path alone and emits a note line beside it; the long description that invited a
trailing sentence is gone, asserted as an absence rather than inferred; and the
first-line convention the template already stated survives, so this change
cannot silently drop it.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

# The template states the value is the path and nothing else on the line.
PATH_ALONE_REQUIREMENT = "the path alone, and nothing else on this line"

# The long description that lived inside the placeholder and invited a worker to
# append it after the path. Its absence is the negative half of the change.
INVITING_DESCRIPTION = (
    "from applying the node's declared negative_control mutation, "
    "whose content names that mutation"
)

# The convention the template already carried before this change: the red log's
# own first line repeats the declared mutation verbatim. It must survive.
FIRST_LINE_CONVENTION = (
    "The log's first line repeats the declared mutation verbatim, so a log "
    "that failed for any other reason is refused"
)


def _node() -> TaskNode:
    return TaskNode(
        id="red-log-path-node",
        goal="state in the manifest template that negative_control_log is a path",
        plan="plan-a",
        section="",
        role="implement",
        done_when="the emitted template states the value is the path alone",
        write_paths=["reckon/crew/prompts.py", "tests/test_red_log_is_a_path.py"],
        time_budget="20m",
    )


def _prompt() -> str:
    return compose_prompt(
        node=_node(),
        project="proj",
        worktree="/repo/worktrees/red-log-run",
        working_directory="/repo/worktrees/red-log-run",
        manifest_path="/state/runs/red-log-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_template_states_the_value_is_the_path_alone():
    prompt = _flat(_prompt())

    assert "negative_control_log:" in prompt
    assert PATH_ALONE_REQUIREMENT in prompt


def test_template_emits_a_note_line_beside_the_log_line():
    raw = _prompt()

    assert "negative_control_note:" in raw
    # Beside it: the note sits after the log field and before the next manifest
    # field, so it belongs to the same block rather than a distant heading.
    assert raw.index("negative_control_log:") < raw.index("negative_control_note:")
    assert raw.index("negative_control_note:") < raw.index("baseline_suite:")


def test_template_no_longer_invites_a_trailing_sentence():
    assert INVITING_DESCRIPTION not in _flat(_prompt())


def test_first_line_convention_survives():
    assert FIRST_LINE_CONVENTION in _flat(_prompt())
