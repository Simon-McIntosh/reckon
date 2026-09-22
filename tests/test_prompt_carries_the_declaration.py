"""The declaration a red log must repeat reaches the worker that writes the log.

Promotion matches the node's declared negative control against the delivered red
log's text, so a worker that never saw the declaration can only paraphrase it —
and a paraphrase is refused by the same substring test the prompt gave the worker
nothing to satisfy. The refusal lands after the worker's process has ended, so
the only party who can answer it is the coordinator. These cases measure the
repair at the surface the worker reads: the composed prompt states this node's
declaration string verbatim, states that the red log's first line must repeat it,
tells a node that declares nothing that none was declared rather than dropping
the subject, carries a declaration containing quotes and newlines unaltered, and
selects the branch that requires a red log on the same predicate promotion
applies — whether the node's write paths reach a test path — so a node whose
scope holds no test path is not told it writes a check.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

BLOCK_HEADER = "CONTRACT — THE NEGATIVE CONTROL THIS NODE DECLARES"
# The template line boundary the block sits immediately before, so a case can
# read the block alone rather than the whole prompt.
MANIFEST_BOUNDARY = "MANIFEST (write exactly these keys"
TEST_PATH = "tests/test_guard.py"
DECLARATION = "removing the guard from reckon/crew/prompts.py turns this case red"


def _prompt(*, negative_control: str = "", write_paths: list[str] | None = None) -> str:
    return compose_prompt(
        node=TaskNode(
            id="declaration-node",
            goal="the composed prompt carries the node's declared negative control",
            plan="plan-a",
            section="guard",
            role="implement",
            done_when="the prompt states the declaration the red log must repeat",
            write_paths=[TEST_PATH] if write_paths is None else write_paths,
            time_budget="20m",
            negative_control=negative_control,
        ),
        project="proj",
        worktree="/repo/worktrees/declaration-run",
        working_directory="/repo/worktrees/declaration-run",
        manifest_path="/state/runs/declaration-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _flat(text: str) -> str:
    """Collapse the prompt's manual line-wrapping so a phrase is found wherever
    the composer broke the line."""
    return " ".join(text.split())


def _declaration_block(prompt: str) -> str:
    """The block alone, up to the manifest template it sits beside."""
    assert BLOCK_HEADER in prompt
    return prompt.split(BLOCK_HEADER, 1)[1].split(MANIFEST_BOUNDARY, 1)[0]


# ── The declaration itself reaches the prompt ─────────────────────────────


def test_the_prompt_carries_the_declaration_string_exactly():
    prompt = _prompt(negative_control=DECLARATION)

    assert DECLARATION in prompt
    assert prompt.count(DECLARATION) == 1


def test_the_prompt_states_that_string_is_what_the_red_log_first_line_repeats():
    prompt = _flat(_declaration_block(_prompt(negative_control=DECLARATION)))

    assert "red log's first line must repeat" in prompt
    assert "verbatim" in prompt


def test_the_declaration_sits_beside_the_requirement_it_answers():
    prompt = _prompt(negative_control=DECLARATION)
    gloss = next(
        line
        for line in prompt.splitlines()
        if line.strip().startswith("negative_control_log:")
    )

    # The block precedes the manifest template, and the template's own gloss
    # still states the manifest requirement the declaration is the answer to.
    assert prompt.index(BLOCK_HEADER) < prompt.index(MANIFEST_BOUNDARY)
    assert prompt.index(DECLARATION) < prompt.index("negative_control_log:")
    assert "first line" in gloss
    assert "verbatim" in gloss
    assert "declared mutation" in gloss


# ── A node that declares nothing keeps the subject ────────────────────────


def test_a_node_with_no_declaration_is_told_so_rather_than_omitting_the_subject():
    prompt = _flat(_prompt(negative_control=""))

    assert BLOCK_HEADER in prompt
    assert "None was declared on this node" in prompt
    # The subject is present but no declaration string is claimed, so the
    # absence reads as a declaration rather than as a gap.
    assert "Declared string:" not in prompt


def test_a_node_that_declares_no_applicable_mutation_is_told_no_log_is_required():
    prompt = _flat(_prompt(negative_control="none: the guard is a constant fold"))

    assert "no mutation applies" in prompt
    assert "no red log is required" in prompt
    assert "none: the guard is a constant fold" in prompt


# ── The declaration survives as written ───────────────────────────────────


def test_a_declaration_with_quotes_and_newlines_reaches_the_prompt_unaltered():
    declaration = (
        'drop the "guard" branch:\n'
        "  revert `if x:` to `if True:`\n"
        "  and the case reddens"
    )
    prompt = _prompt(negative_control=declaration)

    assert declaration in prompt
    # Unaltered means neither reflowed nor re-indented on insertion.
    assert f"  {declaration}\n" in prompt


# ── The declared branch matches the predicate promotion applies ───────────
#
# Promotion discharges a declared mutation only for a node whose write paths
# reach a test path; a node whose paths hold none is exempted with
# `node-writes-no-test-path` before the declaration is read. The branch is
# selected on that same predicate, so a prompt never tells a worker a property
# of its own node that is false.

NO_TEST_PATH = "reckon/crew/prompts.py"
WRITES_A_CHECK = "This node writes a check"
ASKS_FOR_THE_LOG = "Name that log's path in the `negative_control_log` line."


def test_a_declared_mutation_without_a_test_path_neither_claims_a_check_nor_asks_for_a_log():
    block = _flat(
        _declaration_block(
            _prompt(negative_control=DECLARATION, write_paths=[NO_TEST_PATH])
        )
    )

    # The declaration is still rendered — it is the node's record.
    assert DECLARATION in block
    # But the two assertions the exemption makes false are absent.
    assert WRITES_A_CHECK not in block
    assert ASKS_FOR_THE_LOG not in block


def test_a_declared_mutation_with_a_test_path_still_gets_both_assertions():
    block = _flat(_declaration_block(_prompt(negative_control=DECLARATION)))

    assert WRITES_A_CHECK in block
    assert ASKS_FOR_THE_LOG in block


def test_the_none_branch_is_unchanged_by_the_write_paths():
    for write_paths in ([NO_TEST_PATH], [TEST_PATH]):
        block = _flat(
            _declaration_block(
                _prompt(
                    negative_control="none: the guard is a constant fold",
                    write_paths=write_paths,
                )
            )
        )
        assert "no mutation applies" in block
        assert "no red log is required" in block


def test_the_undeclared_branch_is_unchanged_by_the_write_paths():
    block = _flat(
        _declaration_block(_prompt(negative_control="", write_paths=[NO_TEST_PATH]))
    )

    assert "None was declared on this node" in block
    assert "Declared string:" not in block
