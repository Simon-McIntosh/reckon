"""The write-fence rule reaches the worker through the composed dispatch
prompt itself, not through a reference nothing embeds.

The composed prompt deliberately carries no protocol reference at all — it
tells the worker to read the live plan and the named section, and nothing else.
So the fence reaches a worker only if the prompt itself states it, and this
module asserts it now does, for the two roles a node is dispatched as: the rule
that a worker never writes the operator's memory directory or any path outside
its grants, and the reason it holds — the fence enforces the grant, and a write
outside it is refused as a read-only file system. The assertions guard the
addition's wording, that it is purely additive, and that it reaches both roles
and a node with no write paths of its own.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import FENCE_WRITE_GRANT_CONTRACT, compose_prompt

RULE = "Never write the operator's memory directory or any path outside your granted write paths"
REASON_FENCE_ENFORCES = "The fence enforces this"
REASON_REFUSED_READ_ONLY = (
    "a write outside your grants is refused as a read-only file system"
)
# The rule as it appears verbatim in the contract text, including the manual
# line break the composer uses, so the negative control can delete exactly this
# sentence and no other.
RULE_SENTENCE = (
    "Never write the operator's memory directory or any path outside your\n"
    "  granted write paths."
)


def _node(*, write_paths: list[str] | None = None, role: str = "implement") -> TaskNode:
    return TaskNode(
        id="fence-rule-node",
        goal="check the composed prompt carries the write-fence rule",
        plan="plan-a",
        section="",
        role=role,
        done_when="the composed prompt states the fence rule and its reason",
        write_paths=list(write_paths) if write_paths is not None else [],
        time_budget="20m",
    )


def _prompt(*, write_paths: list[str] | None = None, role: str = "implement") -> str:
    return compose_prompt(
        node=_node(write_paths=write_paths, role=role),
        project="proj",
        worktree="/repo/worktrees/fence-run",
        working_directory="/repo/worktrees/fence-run",
        manifest_path="/state/runs/fence-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line-wrapping so a phrase can be found
    regardless of where the composer happened to break the line."""
    return " ".join(text.split())


# ── The rule reaches both roles ─────────────────────────────────────────────


def test_implement_prompt_states_the_fence_rule():
    assert RULE in _flat(_prompt(role="implement"))


def test_review_prompt_states_the_fence_rule():
    assert RULE in _flat(_prompt(role="review"))


# ── The stated reason is the fence and its read-only refusal ────────────────


def test_implement_prompt_states_the_reason_the_rule_holds():
    prompt = _flat(_prompt(role="implement"))

    assert REASON_FENCE_ENFORCES in prompt
    assert REASON_REFUSED_READ_ONLY in prompt


def test_review_prompt_states_the_reason_the_rule_holds():
    prompt = _flat(_prompt(role="review"))

    assert REASON_FENCE_ENFORCES in prompt
    assert REASON_REFUSED_READ_ONLY in prompt


def test_rule_and_reason_sit_in_one_place_in_the_prompt():
    prompt = _flat(_prompt().split("FENCE — WRITE GRANTS", 1)[1])

    assert RULE in prompt
    assert REASON_FENCE_ENFORCES in prompt
    assert REASON_REFUSED_READ_ONLY in prompt


# ── The addition is purely additive ─────────────────────────────────────────


def test_contract_block_is_a_pure_additive_insertion(monkeypatch):
    """Compose the same node with the block masked out and diff against the
    live prompt: removing the contract must reproduce the pre-contract prompt
    exactly, so no other element of the composition changed."""
    import reckon.crew.prompts as prompts_mod

    after = _prompt()
    assert FENCE_WRITE_GRANT_CONTRACT in after
    assert after.count(FENCE_WRITE_GRANT_CONTRACT) == 1

    monkeypatch.setattr(prompts_mod, "FENCE_WRITE_GRANT_CONTRACT", "")
    before = _prompt()

    assert after.replace(FENCE_WRITE_GRANT_CONTRACT, "", 1) == before


# ── The rule is not conditional on the node's shape ─────────────────────────


def test_rule_reaches_a_node_with_no_write_paths():
    prompt = _flat(_prompt(write_paths=[]))

    assert RULE in prompt
    assert REASON_REFUSED_READ_ONLY in prompt


def test_rule_reaches_a_node_with_several_write_paths():
    prompt = _flat(_prompt(write_paths=["reckon/crew/a.py", "tests/test_a.py"]))

    assert RULE in prompt
    assert REASON_REFUSED_READ_ONLY in prompt


def test_rule_holds_only_through_the_declared_contract_text():
    """The rule sentence is stated inside the contract constant and nowhere
    else, so deleting it from that text is the mutation the negative control
    exercises."""
    assert RULE_SENTENCE in FENCE_WRITE_GRANT_CONTRACT
    without_rule = FENCE_WRITE_GRANT_CONTRACT.replace(RULE_SENTENCE, "", 1)
    assert RULE not in _flat(without_rule)
    assert REASON_REFUSED_READ_ONLY in _flat(without_rule)
