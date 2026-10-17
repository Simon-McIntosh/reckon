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
the fragment. The guard asserts the exception itself — every sentence that
mentions the evidence prose must name the fragment AND permit it — rather than
merely that a ban mentions the word "fragment", so a reworded ban that names the
fragment while still forbidding it is rejected. A mutation case pins that
discrimination: it states exactly such a ban and asserts the guard refuses it.

The prompts are composed through the shared builder helpers the small-node suite
already owns, so this module does not restate that composition.
"""

from __future__ import annotations

import re

import pytest

from reckon.crew.prompts import ARTIFACT_NAMED_CLAUSE
from tests.test_small_node_prompt import _flat, _prompt

# The landing instruction the artifact clause must not contradict: the sentence
# names the fragment path the node's write scope grants and the worker is told
# to write its evidence anchor into.
FRAGMENT_INSTRUCTION = (
    "your evidence anchor to your scope's fragment under "
    "docs/evidence/fragments/<plan>/<node-id>.html"
)

# The exception stated in the clause itself: the withheld prose is the prose
# outside the fragment, and the fragment is always permitted. Asserted directly
# so a clause that merely mentions the fragment cannot pass.
FRAGMENT_EXCEPTION = "outside the landing fragment, which is always permitted"

# A sentence that mentions the evidence prose. Every such sentence must except
# the fragment: the fragment is named AND permitted, not merely mentioned. The
# period-terminated run keeps ``docs/evidence/fragments/...html`` from being
# read as one, because no sentence there names "evidence prose".
EVIDENCE_PROSE_SENTENCE = re.compile(r"[^.]*evidence prose[^.]*\.")

# A done-when naming no figure and one naming a figure: the two sides of the
# classification the clause is composed from.
UNNAMED_DONE_WHEN = "the composed prompt carries the rule beside the landing sentence"
NAMED_DONE_WHEN = (
    "the composed prompt names a figure artifact and its wording assertion "
    "fails when the figure sentence is removed"
)

# A ban that names the fragment but forbids it: the reworded defect the guard
# must reject, because the fragment word is present and no permission is.
FRAGMENT_NAMED_THEN_FORBIDDEN = (
    "  Your done-when names no artifact, so this node writes no figure and no\n"
    "  evidence prose, including inside the landing fragment.\n"
)


def _evidence_prose_sentences(text: str) -> list[str]:
    return EVIDENCE_PROSE_SENTENCE.findall(_flat(text))


def _excepts_the_fragment(sentence: str) -> bool:
    """A sentence excepts the fragment only when it names it and permits it."""
    return "fragment" in sentence and "permitted" in sentence


def _assert_no_unexcepted_ban(prompt: str) -> None:
    """Every sentence stating the evidence-prose rule must except the fragment.

    An empty scan would prove nothing, so the rule is confirmed present before
    it is held to the exception.
    """
    sentences = _evidence_prose_sentences(prompt)
    assert sentences, "the prompt states the evidence-prose rule"
    for sentence in sentences:
        assert _excepts_the_fragment(sentence), sentence


@pytest.mark.parametrize(
    "done_when", [UNNAMED_DONE_WHEN, NAMED_DONE_WHEN], ids=["unnamed", "named"]
)
def test_both_prompts_carry_the_landing_fragment_instruction(done_when: str) -> None:
    assert _flat(FRAGMENT_INSTRUCTION) in _flat(_prompt(done_when=done_when))


@pytest.mark.parametrize(
    "done_when", [UNNAMED_DONE_WHEN, NAMED_DONE_WHEN], ids=["unnamed", "named"]
)
def test_both_prompts_permit_the_landing_fragment_at_its_scope_path(
    done_when: str,
) -> None:
    prompt = _flat(_prompt(done_when=done_when))

    # The scope path the exception permits, and the exception itself, both
    # stated in the prompt the worker receives.
    assert _flat(FRAGMENT_INSTRUCTION) in prompt
    assert FRAGMENT_EXCEPTION in prompt


@pytest.mark.parametrize(
    "done_when", [UNNAMED_DONE_WHEN, NAMED_DONE_WHEN], ids=["unnamed", "named"]
)
def test_no_prompt_forbids_evidence_prose_without_excepting_the_fragment(
    done_when: str,
) -> None:
    _assert_no_unexcepted_ban(_prompt(done_when=done_when))


def test_the_named_artifact_clause_excepts_the_fragment_too() -> None:
    """The named clause withholds the prose as well as other figures, so it
    carries the same fragment exception the unnamed clause does."""
    _assert_no_unexcepted_ban(ARTIFACT_NAMED_CLAUSE)


def test_the_guard_rejects_a_ban_that_names_the_fragment_but_forbids_it() -> None:
    """The mutation names the fragment while still forbidding the prose, so the
    fragment word alone must not satisfy the guard: the exception has to be
    stated. The guard's own assertion is made to fail on it."""
    with pytest.raises(AssertionError):
        _assert_no_unexcepted_ban(FRAGMENT_NAMED_THEN_FORBIDDEN)


def test_the_figure_named_prompt_still_grants_its_figure() -> None:
    named = _flat(_prompt(done_when=NAMED_DONE_WHEN))

    assert _flat(ARTIFACT_NAMED_CLAUSE) in named
