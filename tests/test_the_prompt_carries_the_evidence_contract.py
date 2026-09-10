"""The falsifiable-evidence discipline reaches the worker through the
composed dispatch prompt itself, not through a reference nothing embeds.

The composed prompt deliberately carries no protocol reference at all — it
tells the worker to read the live plan and the named section, and nothing
else. So the discipline reaches a worker only if the prompt itself carries it,
and this module asserts it now does: confirm a recorded defect still
reproduces before repairing it, make a guard be shown to fire rather than
trusting a green suite, read the receipt rather than the absence of an error,
treat an implausible measurement as a claim about the instrument, and record
what was changed for a coordinator who cannot see the tree. The assertions
guard the addition's wording, its size, its shape independence, and that it is
purely additive: no other element of the composed prompt changed.
"""

from __future__ import annotations

import re

from reckon.crew.node import TaskNode
from reckon.crew.prompts import FALSIFIABLE_EVIDENCE_CONTRACT, compose_prompt

REPRODUCE_BEFORE_REPAIR = (
    "Confirm a previously recorded defect still reproduces before repairing it and "
    "quote the reproduction"
)
VERIFY_THEN_ADD_TEST = (
    "where the behaviour is already correct, verify it and add the missing test "
    "rather than reimplementing a working guard"
)
GUARD_FIRES_SHOWN = (
    "A passing suite never shows that a guard fires, so make the guarded thing "
    "happen and show the refusal"
)
IMPLAUSIBLE_MEASUREMENT_IS_INSTRUMENT = (
    "Treat an implausible measurement as a claim about the instrument first: a "
    "zero, an empty result or a uniform column needs the check shown to see "
    "something known present before an absence is reported"
)
RECEIPT_NOT_ABSENCE_OF_ERROR = (
    "Read the receipt rather than the absence of an error, and verify a change "
    "landed by a marker it introduced rather than one it removed"
)
UNFILLED_FIELD_READS_AS_NO_WORK = (
    "Record the commits and paths you changed; a coordinator cannot see your "
    "tree, so an unfilled field reads as no work"
)

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
        id="evidence-contract-node",
        goal="check the composed prompt carries the falsifiable-evidence discipline",
        plan="plan-a",
        section="",
        role=role,
        done_when="the composed prompt makes the guarded thing happen and records what changed",
        write_paths=list(write_paths) if write_paths is not None else [],
        time_budget="20m",
    )


def _prompt(*, write_paths: list[str] | None = None, role: str = "implement") -> str:
    return compose_prompt(
        node=_node(write_paths=write_paths, role=role),
        project="proj",
        worktree="/repo/worktrees/evidence-run",
        working_directory="/repo/worktrees/evidence-run",
        manifest_path="/state/runs/evidence-run/manifest.md",
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


# ── All five sentences of the contract are stated ───────────────────────────


def test_prompt_confirms_a_defect_reproduces_before_repairing_it():
    prompt = _flat(_prompt())

    assert REPRODUCE_BEFORE_REPAIR in prompt
    assert VERIFY_THEN_ADD_TEST in prompt


def test_prompt_makes_a_working_guard_be_shown_to_fire():
    prompt = _flat(_prompt())

    assert GUARD_FIRES_SHOWN in prompt


def test_prompt_treats_implausible_measurement_as_instrument_claim():
    prompt = _flat(_prompt())

    assert IMPLAUSIBLE_MEASUREMENT_IS_INSTRUMENT in prompt


def test_prompt_reads_the_receipt_not_the_absence_of_error():
    prompt = _flat(_prompt())

    assert RECEIPT_NOT_ABSENCE_OF_ERROR in prompt


def test_prompt_records_what_the_coordinator_cannot_see():
    prompt = _flat(_prompt())

    assert UNFILLED_FIELD_READS_AS_NO_WORK in prompt


def test_all_five_clauses_sit_in_one_place_in_the_prompt():
    prompt = _flat(_prompt().split("CONTRACT — EVIDENCE THAT COULD HAVE FAILED", 1)[1])

    assert REPRODUCE_BEFORE_REPAIR in prompt
    assert VERIFY_THEN_ADD_TEST in prompt
    assert GUARD_FIRES_SHOWN in prompt
    assert IMPLAUSIBLE_MEASUREMENT_IS_INSTRUMENT in prompt
    assert RECEIPT_NOT_ABSENCE_OF_ERROR in prompt
    assert UNFILLED_FIELD_READS_AS_NO_WORK in prompt


# ── The addition is short, infrastructure-free, and purely additive ─────────


def test_addition_is_at_most_five_sentences():
    assert _sentence_count(FALSIFIABLE_EVIDENCE_CONTRACT) <= 5


def test_addition_names_no_backend_model_or_lane():
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in INFRASTRUCTURE_NAMES) + r")\b",
        re.IGNORECASE,
    )

    assert not pattern.search(FALSIFIABLE_EVIDENCE_CONTRACT)


def test_contract_block_is_a_pure_additive_insertion(monkeypatch):
    """Compose the same node with the block masked out and diff against the
    live prompt: removing the contract must reproduce the pre-contract prompt
    exactly, so no other element of the composition changed."""
    import reckon.crew.prompts as prompts_mod

    after = _prompt()
    assert FALSIFIABLE_EVIDENCE_CONTRACT in after
    assert after.count(FALSIFIABLE_EVIDENCE_CONTRACT) == 1

    monkeypatch.setattr(prompts_mod, "FALSIFIABLE_EVIDENCE_CONTRACT", "")
    before = _prompt()

    assert after.replace(FALSIFIABLE_EVIDENCE_CONTRACT, "", 1) == before


def test_prompt_carries_no_reference_document_path():
    prompt = _flat(_prompt())

    for path in REFERENCE_DOC_PATHS:
        assert path not in prompt


# ── The contract is not conditional on the node's shape ─────────────────────


def test_contract_reaches_a_node_with_no_write_paths():
    prompt = _flat(_prompt(write_paths=[]))

    assert REPRODUCE_BEFORE_REPAIR in prompt
    assert GUARD_FIRES_SHOWN in prompt
    assert UNFILLED_FIELD_READS_AS_NO_WORK in prompt


def test_contract_reaches_a_node_with_several_write_paths():
    prompt = _flat(
        _prompt(write_paths=["reckon/crew/a.py", "reckon/crew/b.py", "tests/test_a.py"])
    )

    assert REPRODUCE_BEFORE_REPAIR in prompt
    assert GUARD_FIRES_SHOWN in prompt
    assert UNFILLED_FIELD_READS_AS_NO_WORK in prompt


def test_contract_reaches_every_role():
    for role in ("implement", "test", "review", "investigate"):
        prompt = _flat(_prompt(role=role))
        assert REPRODUCE_BEFORE_REPAIR in prompt
        assert GUARD_FIRES_SHOWN in prompt
        assert UNFILLED_FIELD_READS_AS_NO_WORK in prompt
