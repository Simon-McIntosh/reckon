"""The contract the review prompt makes with its reviewer, and its limits.

An independent review is worth having only if it is quick enough to be
reflexive, so the prompt bounds what the reviewer reads as well as requiring a
verdict for every checklist item. Both halves are prompt text, and text drifts
silently: nothing fails when a bound stops being stated, or when the module
that carries it starts claiming it is not on any live path. The falsifiers here
read the prompt and the module as files and assert the claims that hold them.

The prompt also asks the reviewer to check that each assertion a run added can
actually fail, because an assertion true by construction passes, reddens no
control, and reports a guarantee that is not there. That instruction is a
sentence of prompt text like any other, so it is held by the same falsifier.

The other half of the contract is a limit: per-item verdicts are recorded and
their absence named, but they must NOT enter the completeness predicate a
promotion reads. Reviews stored before the verdict line existed carry none, and
folding the items into that predicate would mark every one of them incomplete.
That is asserted here by calling the predicate, because asserting it only via
the parser would pass whether or not the predicate had moved.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import recovery
from reckon.crew import review as review_module

# The three things the prompt tells the reviewer not to do. Each is one
# exclusion of the reading bound; the bound is only stated if all three are.
READING_BOUND_EXCLUSIONS = (
    "do not re-derive the implementation",
    "do not re-run the full suite",
    "do not review code the node did not touch",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The rule that an added assertion must be able to fail. The review is the
# only instrument that catches an assertion true by construction — it passes,
# the control reddens, and the guarantee is absent — so the prompt has to ask
# the question, name the independent-source shape, and state the remedy. Each
# phrase is load-bearing: a rewrite that drops one is a prompt that no longer
# asks, and nothing else in the gate stack would notice.
ASSERTION_INDEPENDENCE_PHRASES = (
    "what would have to be true of the code for that assertion to fail",
    (
        "the same variable the test itself passed in rather than against an "
        "independent source"
    ),
    (
        "an assertion added to prevent a regression is verified by reinstating "
        "the regression, not by observing the test pass"
    ),
)

# The two sentences the prompt must carry so the review judges reuse. The first
# is the fit clause: a new definition whose capability an existing owner already
# provides, and which does not extend that owner, is a fit defect that scores
# below the tier floor and emits a FINDING naming the owner. The second is the
# reading instruction that lets the reviewer find that owner: one read outside
# the diff, by capability rather than by name. Both are held below the same
# falsifier, and the control asserts the falsifier is sensitive to their
# deletion rather than merely present.
REUSE_FIT_CLAUSE = (
    "A change that adds a function, class or module whose capability the "
    "repository already owns, without extending that owner, scores fit below "
    "10, and the review emits a FINDING naming the owning file and whether the "
    "addition extends, wraps or duplicates it"
)
REUSE_SEARCH_CLAUSE = (
    "For each function, class or module the diff adds, search the repository "
    "for an existing mechanism with the same capability — search by capability, "
    "not by name — and open the owner you name"
)
REUSE_CLAUSE_SENTENCE_NOTE = (
    "the prompt no longer states the reuse judgement the review must make"
)


def _states_the_reuse_judgement(prompt: str) -> bool:
    collapsed = " ".join(prompt.split())
    return REUSE_FIT_CLAUSE in collapsed and REUSE_SEARCH_CLAUSE in collapsed


def _without_the_reuse_clause(prompt: str) -> str:
    collapsed = " ".join(prompt.split())
    return collapsed.replace(REUSE_FIT_CLAUSE, "").replace(REUSE_SEARCH_CLAUSE, "")


DIMENSION_ONLY_REVIEW = "\n".join(
    f"SCORE {dimension}: 12" for dimension in review_module.REVIEW_DIMENSIONS
)


def test_prompt_states_the_reading_bound_with_all_three_exclusions() -> None:
    loaded = review_module.load_review_prompt().lower()
    for exclusion in READING_BOUND_EXCLUSIONS:
        assert exclusion in loaded, (
            f"the prompt no longer states the reading bound: {exclusion!r}"
        )


def test_prompt_requires_an_added_assertion_to_be_able_to_fail() -> None:
    # Whitespace is collapsed because the clauses wrap across lines in the
    # prompt; a line-wrapped clause is still one instruction to the reviewer.
    prompt = " ".join(review_module.load_review_prompt().split())
    for phrase in ASSERTION_INDEPENDENCE_PHRASES:
        assert phrase in prompt, (
            "the prompt no longer asks the reviewer to check that an added "
            f"assertion can fail: {phrase!r} is missing"
        )


def test_prompt_requires_the_review_to_judge_reuse() -> None:
    prompt = review_module.load_review_prompt()
    assert _states_the_reuse_judgement(prompt), REUSE_CLAUSE_SENTENCE_NOTE
    stripped = _without_the_reuse_clause(prompt)
    assert not _states_the_reuse_judgement(stripped), (
        "the reuse check passes on the prompt with the clause deleted, so it "
        "does not hold the reuse text it claims to"
    )


def test_review_module_no_longer_claims_it_is_unwired() -> None:
    source = (REPO_ROOT / "reckon" / "crew" / "review.py").read_text(encoding="utf-8")
    assert "Nothing here is wired into dispatch" not in source, (
        "the module docstring still says the parser is not on any live path"
    )
    # And the claim it should make instead: the call sites that reach it.
    for caller in ("promotion.py", "recovery.py"):
        assert caller in source, (
            f"the docstring no longer names {caller} among the call sites"
        )


def test_a_dimension_only_review_is_still_complete_for_promotion() -> None:
    record = review_module.parse_review(DIMENSION_ONLY_REVIEW)
    # The review genuinely carries no item verdicts, so the predicate's True
    # below is about the items not being required, not about them being there.
    assert record["absent_items"] == list(review_module.REVIEW_ITEMS)
    assert record["item_aggregate"] is None
    assert record["absent"] == []
    assert recovery._review_is_complete(record) is True


def test_a_review_missing_a_dimension_stays_incomplete_alongside_the_items() -> None:
    partial = "\n".join(
        f"SCORE {dimension}: 12"
        for dimension in review_module.REVIEW_DIMENSIONS
        if dimension != "fit"
    )
    record = review_module.parse_review(partial)
    assert record["absent"] == ["fit"]
    assert recovery._review_is_complete(record) is False
