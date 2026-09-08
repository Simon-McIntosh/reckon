"""The commit-and-manifest-early contract reaches the worker through the
composed dispatch prompt itself, not through a reference nothing embeds.

The durable-write discipline lives in a reference document, and the composed
prompt deliberately carries no protocol reference at all — it tells the worker
to read the live plan and the named section, and nothing else. So the contract
reaches a worker only if the prompt itself carries it, and this module asserts
it now does: the worker commits each deliverable as it completes rather than
once at the end, and writes its manifest before beginning any long output. The
assertions guard the addition's wording, its size, its shape independence, and
that it is purely additive: no other element of the composed prompt changed.
"""

from __future__ import annotations

import re

from reckon.crew.node import TaskNode
from reckon.crew.prompts import DURABLE_WRITE_CONTRACT, compose_prompt

COMMIT_EACH_DELIVERABLE = (
    "Commit each deliverable as it completes rather than once at the end"
)
MANIFEST_BEFORE_LONG_OUTPUT = (
    "write your manifest carrying whatever you already hold before beginning "
    "any long output"
)
RECOVERY_REASON = "This is recovery, not death prevention"
PHYSICS_CLAIM = "survives that generation failing"

# Backends, models and lanes this workstation dispatches to; none may appear in
# the added contract text, which must state a mechanical rule, not route work.
INFRASTRUCTURE_NAMES = (
    "claude",
    "codex",
    "glm",
    "gpt",
    "sonnet",
    "opus",
    "clive",
    "spark",
    "flash",
    "astra",
    "luna",
    "terra",
    "betelgeuse",
    "debug",
    "deepseek",
)
REFERENCE_DOC_PATHS = ("references/", "worker-protocol.md", "sprint-orchestration.md")


def _node(*, write_paths: list[str] | None = None, role: str = "implement") -> TaskNode:
    return TaskNode(
        id="contract-node",
        goal="check the composed prompt carries the durable-write contract",
        plan="plan-a",
        section="",
        role=role,
        done_when="the composed prompt commits each deliverable and writes the manifest before long output",
        write_paths=list(write_paths) if write_paths is not None else [],
        time_budget="20m",
    )


def _prompt(*, write_paths: list[str] | None = None, role: str = "implement") -> str:
    return compose_prompt(
        node=_node(write_paths=write_paths, role=role),
        project="proj",
        worktree="/repo/worktrees/contract-run",
        working_directory="/repo/worktrees/contract-run",
        manifest_path="/state/runs/contract-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line-wrapping so a phrase can be found
    regardless of where the composer happened to break the line."""
    return " ".join(text.split())


def _sentence_count(text: str) -> int:
    """Count terminal-punctuation sentences; a section label carries none."""
    return len(re.findall(r"[.!?](?:\s|$)", text))


# ── Both halves of the contract are stated ─────────────────────────────────


def test_prompt_commits_each_deliverable_as_it_completes():
    prompt = _flat(_prompt())

    assert COMMIT_EACH_DELIVERABLE in prompt


def test_prompt_writes_the_manifest_before_long_output():
    prompt = _flat(_prompt())

    assert MANIFEST_BEFORE_LONG_OUTPUT in prompt


def test_both_instructions_sit_in_one_place_in_the_prompt():
    prompt = _flat(_prompt().split("CONTRACT — DURABLE WRITES", 1)[1])

    assert COMMIT_EACH_DELIVERABLE in prompt
    assert MANIFEST_BEFORE_LONG_OUTPUT in prompt


# ── The stated reason is recovery, never prevention ────────────────────────


def test_reason_is_recovery_not_death_prevention():
    assert RECOVERY_REASON in DURABLE_WRITE_CONTRACT
    assert PHYSICS_CLAIM in DURABLE_WRITE_CONTRACT


def test_no_claim_that_the_practice_prevents_or_improves():
    addition = _flat(DURABLE_WRITE_CONTRACT)

    assert "prevents" not in addition
    assert "improves" not in addition
    assert "chance of finishing" not in addition
    assert "does not" not in addition


# ── The addition is short, infrastructure-free, and purely additive ────────


def test_addition_is_at_most_four_sentences():
    assert _sentence_count(DURABLE_WRITE_CONTRACT) <= 4


def test_addition_names_no_backend_model_or_lane():
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in INFRASTRUCTURE_NAMES) + r")\b",
        re.IGNORECASE,
    )

    assert not pattern.search(DURABLE_WRITE_CONTRACT)


def test_contract_block_is_a_pure_additive_insertion(monkeypatch):
    """Compose the same node with the block masked out and diff against the
    live prompt: removing the contract must reproduce the pre-contract prompt
    exactly, so no other element of the composition changed."""
    import reckon.crew.prompts as prompts_mod

    after = _prompt()
    assert DURABLE_WRITE_CONTRACT in after
    assert after.count(DURABLE_WRITE_CONTRACT) == 1

    monkeypatch.setattr(prompts_mod, "DURABLE_WRITE_CONTRACT", "")
    before = _prompt()

    assert after.replace(DURABLE_WRITE_CONTRACT, "", 1) == before


def test_prompt_carries_no_reference_document_path():
    prompt = _flat(_prompt())

    for path in REFERENCE_DOC_PATHS:
        assert path not in prompt


# ── The contract is not conditional on the node's shape ────────────────────


def test_contract_reaches_a_node_with_no_write_paths():
    prompt = _flat(_prompt(write_paths=[]))

    assert COMMIT_EACH_DELIVERABLE in prompt
    assert MANIFEST_BEFORE_LONG_OUTPUT in prompt


def test_contract_reaches_a_node_with_several_write_paths():
    prompt = _flat(
        _prompt(write_paths=["reckon/crew/a.py", "reckon/crew/b.py", "tests/test_a.py"])
    )

    assert COMMIT_EACH_DELIVERABLE in prompt
    assert MANIFEST_BEFORE_LONG_OUTPUT in prompt


def test_contract_reaches_every_role():
    for role in ("implement", "test", "review", "investigate"):
        prompt = _flat(_prompt(role=role))
        assert COMMIT_EACH_DELIVERABLE in prompt
        assert MANIFEST_BEFORE_LONG_OUTPUT in prompt
