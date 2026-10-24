"""A shadow's review brief names its retained worktree patch, not a commit diff.

A shadow is dispatched to produce evidence without committing: its head equals
its base and its commits list is empty, and the deliverable promotion preserves
is the worktree patch it leaves behind. The landed-node rubric tells a reviewer
to read the diff of the run's commits against its base, so a shadow reviewed on
that rubric alone reads an empty diff and scores the change that is not there.
The composed brief therefore appends a shadow block naming the two patch
commands, stating that the empty commit list is the contract, and binding every
dimension to the patch. A run that is not a shadow keeps the brief it had, and a
paired primary — which names a shadow but is itself committed work — is not a
shadow and gets no block.

These cases compose the brief through
:func:`reckon.crew.recovery_review_subject._review_brief_text` and assert the
composed text directly. They write nothing outside the temporary directory.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import recovery_review_subject
from reckon.crew.review import load_review_prompt

BASE_SHA = "a5985da3f2185f2b0f5d6c2a3b4c5d6e7f8091a2"
HEAD = "a5985da3f2185f2b0f5d6c2a3b4c5d6e7f8091a2"

# The identity lines the brief composes for the non-shadow record, spelled here
# rather than read from the composer so the expectation is independent of the
# value the code under test builds.
NON_SHADOW_IDENTITY = f"Reviewed run: r-plain-run\nReviewed head: {HEAD}\n"

# The review record contract every composed brief carries, spelled here rather
# than read from the composer for the same reason the identity is: a change to
# the contract the fleet states must turn this red instead of moving both sides
# together. It is the block that follows the identity, not one a shadow adds.
NON_SHADOW_RECORD_CONTRACT = (
    "Review record contract (run crew check --review --record <path> on each written record before ending):\n"
    "Required: project, reviewed_run_id, review_run_id, reviewed_base_sha, reviewed_head_sha,\n"
    "scores (goal_fidelity, evidence, scope_discipline, durability, fit, reuse),\n"
    "added_failure_count, added_failure_ids, findings, status.\n"
    "Accepted status values: incomplete, parsed, unparsed.\n"
    "Use parsed when the rubric produced a readable review; unparsed when it did not;\n"
    "incomplete when the review has not finished and no verdict can be acted on."
)

SHADOW_CONTRACT_SENTENCE = (
    "An empty commits list and a head equal to the base are this run's "
    "contract, not a finding."
)


def _shadow_record(worktree: Path, base_sha: str = BASE_SHA) -> dict:
    """A record whose lineage marks it as shadow evidence of a primary run."""
    return {
        "run_id": "r-shadow-arm",
        "project": "sample",
        "worktree": str(worktree),
        "base_sha": base_sha,
        "lineage": {"kind": "shadow", "primary_run_id": "r-primary-arm"},
        "node": {"id": "shadow-arm", "plan": "", "section": ""},
    }


def _plain_record(worktree: Path) -> dict:
    """A record that is not a shadow and carries no shadow lineage."""
    return {
        "run_id": "r-plain-run",
        "project": "sample",
        "repo": str(worktree),
        "node": {"id": "plain-arm", "plan": "", "section": ""},
    }


def _paired_primary_record(worktree: Path) -> dict:
    """A primary that names a shadow partner but is committed work itself."""
    return {
        "run_id": "r-primary-arm",
        "project": "sample",
        "repo": str(worktree),
        "paired_shadow_run_id": "r-shadow-arm",
        "node": {"id": "paired-arm", "plan": "", "section": ""},
    }


def test_a_shadow_brief_names_the_retained_patch(tmp_path: Path) -> None:
    """The shadow block names the patch commands built from worktree and base."""
    worktree = tmp_path / "shadow-arm-worktree"

    text = recovery_review_subject._review_brief_text(_shadow_record(worktree), HEAD)

    assert f"git -C {worktree} diff {BASE_SHA}" in text
    assert f"git -C {worktree} ls-files --others --exclude-standard" in text
    assert SHADOW_CONTRACT_SENTENCE in text
    assert "shadow" in text


def test_the_shadow_block_reads_worktree_and_base_from_the_record(
    tmp_path: Path,
) -> None:
    """A different record's worktree and base appear in the composed commands."""
    worktree = tmp_path / "shadow-arm-worktree"
    other_tree = tmp_path / "another-shadow-arm"
    other_base = "0123456789abcdef0123456789abcdef01234567"

    text = recovery_review_subject._review_brief_text(
        _shadow_record(other_tree, other_base), other_base
    )

    assert str(other_tree) in text
    assert other_base in text
    assert str(worktree) not in text
    assert BASE_SHA not in text


def test_a_run_that_is_not_a_shadow_keeps_todays_brief(tmp_path: Path) -> None:
    """A non-shadow brief is the rubric, the identity, and the record contract."""
    record = _plain_record(tmp_path)

    text = recovery_review_subject._review_brief_text(record, HEAD)

    expected = (
        load_review_prompt().rstrip("\n")
        + "\n\n"
        + NON_SHADOW_IDENTITY
        + "\n"
        + NON_SHADOW_RECORD_CONTRACT
        + "\n"
    )
    assert text == expected
    assert "shadow" not in text
    assert SHADOW_CONTRACT_SENTENCE not in text


def test_a_paired_primary_gets_no_shadow_block(tmp_path: Path) -> None:
    """Naming a shadow partner does not make the primary a shadow."""
    record = _paired_primary_record(tmp_path)

    text = recovery_review_subject._review_brief_text(record, HEAD)

    assert "shadow" not in text
    assert SHADOW_CONTRACT_SENTENCE not in text
