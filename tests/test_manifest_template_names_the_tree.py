"""The manifest template asks a measurement to name the tree it imported.

A base arm measured in a scratch copy carried no in-process provenance: its log
named a revision in the header while its body held no module path and no working
directory, so which tree served the module under test was the header's claim
about a copy whose construction was not in the record. Counts cannot close that
gap, because the shared cases pass under either revision and a copy importing
stale bytecode leaves the failure-id sets looking identical. The template is the
contract a worker actually reads, so both facts are asked for there: on the
sentence that already says what makes a log evidence, scoped to a measurement run
in a scratch tree (a fact that carries no information for a single-tree run is a
rule read as noise), and as keys beside ``test_logs`` so a reader who never opens
the log still sees which tree served the measurement. The existing sentence must
survive exactly once, because extending it is the point and restating it beside
itself would hand one reader two rules to reconcile.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

REVISION_HEADER_SENTENCE = (
    "A gate log's first line names the revision it ran at, the tree, and the command"
)
MODULE_PATH_REQUEST = "the absolute path of the module under test as imported"
WORKING_DIRECTORY_REQUEST = "the resolved working directory"
SCRATCH_TREE_SCOPE = "measurement run in a scratch tree"
MODULE_KEY = "measurement_module"
WORKING_DIRECTORY_KEY = "measurement_cwd"
MANIFEST_HEADER = "MANIFEST (write exactly these keys"
WORKTREE_RULES_MARKER = "WORKTREE AND PARALLEL-SAFETY RULES"


def _node() -> TaskNode:
    return TaskNode(
        id="manifest-tree-node",
        goal="the manifest template asks a measurement to name the tree it imported",
        plan="plan-a",
        section="",
        role="implement",
        done_when=(
            "the emitted template asks a scratch-tree log for the module path and "
            "the working directory, and offers a manifest key for each"
        ),
        write_paths=[
            "reckon/crew/prompts.py",
            "tests/test_manifest_template_names_the_tree.py",
        ],
        time_budget="20m",
    )


def _prompt() -> str:
    return compose_prompt(
        node=_node(),
        project="proj",
        worktree="/repo/worktrees/manifest-tree-run",
        working_directory="/repo/worktrees/manifest-tree-run",
        manifest_path="/state/runs/manifest-tree-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _manifest_block(prompt: str) -> list[str]:
    _, rest = prompt.split(MANIFEST_HEADER, 1)
    body, _ = rest.split(WORKTREE_RULES_MARKER, 1)
    return body.splitlines()


def _manifest_gloss(prompt: str, key: str) -> str:
    """The manifest gloss line a worker reads for ``key``, read out of the
    composed prompt so the assertions measure the rendered contract."""
    for line in prompt.splitlines():
        if line.startswith("  ") and line.strip().startswith(key + ":"):
            return " ".join(line.split())
    raise AssertionError(f"the manifest template emits no {key!r} gloss line")


def _block_order(prompt: str) -> list[str]:
    return [
        line.strip().split(":", 1)[0] for line in _manifest_block(prompt) if ":" in line
    ]


def test_the_log_sentence_asks_a_scratch_tree_measurement_to_name_its_module_path():
    gloss = _manifest_gloss(_prompt(), "test_logs")

    assert MODULE_PATH_REQUEST in gloss
    assert "`module.__file__`" in gloss


def test_the_log_sentence_asks_a_scratch_tree_measurement_to_name_its_working_directory():
    gloss = _manifest_gloss(_prompt(), "test_logs")

    assert WORKING_DIRECTORY_REQUEST in gloss


def test_the_request_is_scoped_to_a_scratch_tree_rather_than_every_log():
    gloss = _manifest_gloss(_prompt(), "test_logs")

    assert SCRATCH_TREE_SCOPE in gloss


def test_the_manifest_block_offers_a_key_for_the_module_path():
    gloss = _manifest_gloss(_prompt(), MODULE_KEY)

    assert MODULE_PATH_REQUEST in gloss
    assert "`module.__file__`" in gloss


def test_the_manifest_block_offers_a_key_for_the_working_directory():
    gloss = _manifest_gloss(_prompt(), WORKING_DIRECTORY_KEY)

    assert WORKING_DIRECTORY_REQUEST in gloss
    assert SCRATCH_TREE_SCOPE in gloss


def test_the_two_keys_sit_beside_the_log_field_they_belong_to():
    order = _block_order(_prompt())

    assert (
        order.index("test_logs")
        < order.index(MODULE_KEY)
        < order.index(WORKING_DIRECTORY_KEY)
        < order.index("negative_control_log")
    )


def test_the_revision_header_sentence_survives_exactly_once():
    assert _prompt().count(REVISION_HEADER_SENTENCE) == 1
