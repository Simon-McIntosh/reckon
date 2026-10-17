"""An implement prompt never both asks for its landing record and forbids it.

The composed prompt carries two clauses that met as a contradiction: the landing
contract asks the worker to write its evidence anchor into its scope's fragment
under ``docs/evidence/fragments/<plan>/<node-id>.html``, and the artifact clause
said the node "writes no figure and no evidence prose". A worker following the
first wrote the fragment, a reviewer enforcing the second sent it back, and one
repair round made a worker delete its own landing record. A fragment is evidence
prose, so the two clauses cannot both hold unqualified.

The rule this module asserts is a single one: the landing fragment is the node's
evidence anchor and is always permitted at its scope path; a figure still needs
the done-when to name it, and the withheld evidence prose is the prose OUTSIDE
the fragment. The clauses are asserted by their own wording and by the rule they
have to satisfy together rather than by a snapshot, so a rewording that keeps
the rule passes and a rewording that reinstates the ban fails.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.prompts import (
    ARTIFACT_NAMED_CLAUSE,
    ARTIFACT_UNNAMED_CLAUSE,
    compose_prompt,
)

# The landing instruction the artifact clause must not contradict: the sentence
# names the fragment path the node's write scope grants and the worker is told
# to write its evidence anchor into.
FRAGMENT_INSTRUCTION = (
    "your evidence anchor to your scope's fragment under "
    "docs/evidence/fragments/<plan>/<node-id>.html"
)

# A sentence that mentions the evidence prose. Every such sentence must except
# the landing fragment: the fragment is excluded from the ban, so a sentence
# stating the ban without the exception is exactly the contradiction this node
# retires. The period-terminated run keeps ``docs/evidence/fragments/...html``
# from being read as one, because no sentence there names "evidence prose".
EVIDENCE_PROSE_SENTENCE = re.compile(r"[^.]*evidence prose[^.]*\.")

# A done-when naming no figure and one naming a figure: the two sides of the
# classification the clause is composed from.
UNNAMED_DONE_WHEN = "the composed prompt carries the rule beside the landing sentence"
NAMED_DONE_WHEN = (
    "the composed prompt names a figure artifact and its wording assertion "
    "fails when the figure sentence is removed"
)


def _compose(done_when: str) -> str:
    node = TaskNode(
        id="landing-node",
        goal="compose the implement prompt for a node",
        plan="plan-a",
        section="s3",
        role="implement",
        done_when=done_when,
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )
    return compose_prompt(
        node=node,
        project="proj",
        worktree="/repo/worktree",
        working_directory="/repo/worktree",
        manifest_path="/state/runs/landing-node/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line wrapping so a sentence reads whole
    regardless of where the composer broke the line."""
    return " ".join(text.split())


def _evidence_prose_sentences(text: str) -> list[str]:
    return EVIDENCE_PROSE_SENTENCE.findall(_flat(text))


@pytest.mark.parametrize(
    "done_when", [UNNAMED_DONE_WHEN, NAMED_DONE_WHEN], ids=["unnamed", "named"]
)
def test_both_prompts_carry_the_landing_fragment_instruction(done_when: str) -> None:
    assert _flat(FRAGMENT_INSTRUCTION) in _flat(_compose(done_when))


@pytest.mark.parametrize(
    "done_when", [UNNAMED_DONE_WHEN, NAMED_DONE_WHEN], ids=["unnamed", "named"]
)
def test_no_prompt_forbids_evidence_prose_without_excepting_the_fragment(
    done_when: str,
) -> None:
    sentences = _evidence_prose_sentences(_compose(done_when))

    # An empty scan would prove nothing: confirm the rule is stated, then hold
    # every sentence that states it to the exception.
    assert sentences, "the prompt states the evidence-prose rule"
    for sentence in sentences:
        assert "fragment" in sentence, sentence


def test_the_named_artifact_clause_excepts_the_fragment_too() -> None:
    """The named clause withholds the prose as well as other figures, so it
    carries the same fragment exception the unnamed clause does."""
    sentences = _evidence_prose_sentences(ARTIFACT_NAMED_CLAUSE)

    assert sentences, "the named clause states the evidence-prose rule"
    for sentence in sentences:
        assert "fragment" in sentence, sentence


def test_the_figure_named_prompt_still_grants_its_figure() -> None:
    named = _flat(_compose(NAMED_DONE_WHEN))
    unnamed = _flat(_compose(UNNAMED_DONE_WHEN))

    assert _flat(ARTIFACT_NAMED_CLAUSE) in named
    assert _flat(ARTIFACT_UNNAMED_CLAUSE) not in named
    assert _flat(ARTIFACT_UNNAMED_CLAUSE) in unnamed
    assert _flat(ARTIFACT_NAMED_CLAUSE) not in unnamed
