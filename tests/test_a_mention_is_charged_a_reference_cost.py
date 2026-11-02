"""A brief's mention of a repository path costs a fixed reference, not its size.

A brief names files it does not read: the anchor an edit works around, and the
module a prohibition keeps a worker away from. Pricing those at file size made a
brief undispatchable for naming a large module, so the incentive was to stop
writing the prohibition. These pin the replacement rule -- a mention costs one
flat reference and a declared write path still costs its full size -- and the
three briefs recorded against the old rule, which must fit their lanes under the
new one.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import routing
from reckon.crew.routing import _context_file_inputs

PROJECT = "sample"
PLAN = "context-references"
PLAN_PATH = f"docs/plans/{PLAN}.html"
REFERENCE_TOKENS = routing._REFERENCE_COST_TOKENS
BYTES_PER_TOKEN = 3.5
WINDOW = 480_000

ANCHOR_PATH = "imas_codex/standard_names/graph_ops.py"
PROHIBITED_PATH = "imas_codex/standard_names/graph_ops.py"
WRITE_PATH = "cli/sn.py"
SMALL_WRITE_PATH = "package/target.py"


def _estimated_tokens(byte_count: int) -> int:
    return math.ceil(byte_count / BYTES_PER_TOKEN)


def _bytes_for_tokens(tokens: int) -> int:
    """A byte count the documented estimator returns exactly ``tokens`` for."""

    return int(tokens * BYTES_PER_TOKEN) - 1


def _node(
    *,
    goal: str = "measure a mentioned context input",
    done_when: str = "",
    write_paths: list[str] | None = None,
) -> crew.TaskNode:
    return crew.TaskNode(
        id="context-references",
        goal=goal,
        plan=PLAN,
        section="s3",
        role="implement",
        spec_level="exact",
        done_when=done_when or "the gate is green",
        write_paths=write_paths or [],
        time_budget="10m",
        estimated_hours=1.0,
    )


def _authority(repo: Path) -> dict[str, object]:
    return {
        "plan": {
            "project": PROJECT,
            "repository": str(repo),
            "docs": str(repo / "docs"),
        }
    }


def _seed(repo: Path, paths: list[tuple[str, int]]) -> None:
    plan = repo / PLAN_PATH
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '</head><body><h2 id="s3">Context</h2></body></html>',
        encoding="utf-8",
    )
    for path, byte_count in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * byte_count)


def _file_tokens(repo: Path, node: crew.TaskNode) -> int:
    tokens, _inputs = _context_file_inputs(repo, node, _authority(repo))
    return tokens


def _charge(inputs: dict[str, list[dict[str, object]]], path: str) -> int:
    """What the census charged for one path, whichever group claimed it."""

    for group in ("write_paths", "named_files"):
        for record in inputs[group]:
            if record["declared"] == path and record["counted"]:
                return int(record["estimated_tokens"])
    return 0


def test_a_mention_costs_the_reference_and_not_the_file_size(tmp_path: Path) -> None:
    """One brief twice, identical but for the mention, differs by the reference."""
    _seed(tmp_path, [(ANCHOR_PATH, 1_200_000)])
    named = _node(done_when=f"the anchor for the edit is {ANCHOR_PATH}")
    unnamed = _node(done_when="the anchor for the edit is in the same package")

    named_tokens = _file_tokens(tmp_path, named)
    unnamed_tokens = _file_tokens(tmp_path, unnamed)

    assert unnamed_tokens == 0
    assert named_tokens == REFERENCE_TOKENS
    assert named_tokens - unnamed_tokens == REFERENCE_TOKENS
    assert named_tokens != _estimated_tokens((tmp_path / ANCHOR_PATH).stat().st_size)

    _tokens, inputs = _context_file_inputs(tmp_path, named, _authority(tmp_path))
    records = {str(record["declared"]): record for record in inputs["named_files"]}
    assert records[ANCHOR_PATH]["provenance"] == "reference"
    assert records[ANCHOR_PATH]["reference_tokens"] == REFERENCE_TOKENS
    assert records[ANCHOR_PATH]["bytes"] > 1_000_000


def test_a_path_in_a_negative_clause_is_charged_as_a_reference(tmp_path: Path) -> None:
    """The one case the estimator can be certain of is charged as a reference."""
    _seed(tmp_path, [(PROHIBITED_PATH, 1_200_000), (SMALL_WRITE_PATH, 10)])
    node = _node(
        write_paths=[SMALL_WRITE_PATH],
        done_when=(
            f"do not edit {PROHIBITED_PATH} under any circumstances; "
            "the marker file is the only scope"
        ),
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))

    assert _charge(inputs, PROHIBITED_PATH) == REFERENCE_TOKENS
    assert _charge(inputs, SMALL_WRITE_PATH) == _estimated_tokens(10)
    assert tokens == REFERENCE_TOKENS + _estimated_tokens(10)


def test_a_negative_clause_does_not_discount_the_write_path_it_names(
    tmp_path: Path,
) -> None:
    """The write path stays a load even when a prohibition also names the path.

    A node cannot both need to edit a file and be told never to open it, so the
    brief is contradictory; the census resolves it toward the load, which is the
    larger charge and so cannot let a contradictory brief through cheaply.
    """
    _seed(tmp_path, [(WRITE_PATH, 400_000)])
    node = _node(
        write_paths=[WRITE_PATH],
        done_when=f"never read {WRITE_PATH} before overwriting it",
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))

    full = _estimated_tokens(400_000)
    assert _charge(inputs, WRITE_PATH) == full
    assert tokens == full


def test_a_declared_input_is_still_charged_at_full(tmp_path: Path) -> None:
    """The reference cost is a rule about mentions, not a blanket discount."""
    _seed(tmp_path, [(ANCHOR_PATH, 1_200_000)])
    node = _node(
        done_when=f"treat {ANCHOR_PATH} as a declared input to this node",
    )

    tokens, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))

    full = _estimated_tokens((tmp_path / ANCHOR_PATH).stat().st_size)
    assert _charge(inputs, ANCHOR_PATH) == full
    assert tokens == full


def test_a_brief_naming_no_repository_path_estimates_unchanged(tmp_path: Path) -> None:
    """A path-free brief pays nothing, so the change is not a blanket charge."""
    _seed(tmp_path, [(ANCHOR_PATH, 1_200_000)])
    plain = _node(done_when="the gate is green and the manifest is written")
    other_words = _node(
        goal="measure an unrelated input",
        done_when="every check names its own subject and its own result",
    )

    assert _file_tokens(tmp_path, plain) == 0
    assert _file_tokens(tmp_path, other_words) == 0
    _tokens, inputs = _context_file_inputs(tmp_path, plain, _authority(tmp_path))
    assert inputs["named_files"] == []
    assert inputs["write_paths"] == []


# The three shapes recorded from refusal payloads in the plan, at the figures
# those payloads carried. ``named_tokens`` is the contribution the brief's named
# file made, and ``total`` the estimate the refusal reported.
MEASURED_BRIEFS = (
    {
        "row": "declared write path",
        "path": WRITE_PATH,
        "named_tokens": 90_281,
        "total": 161_013,
        "write_path": True,
        "done_when": "the surface file the node edits is the only scope",
    },
    {
        "row": "anchor named in prose",
        "path": ANCHOR_PATH,
        "named_tokens": 326_217,
        "total": 326_217,
        "write_path": False,
        "done_when": "the edit is anchored on the module named above",
    },
    {
        "row": "prohibition",
        "path": ANCHOR_PATH,
        "named_tokens": 326_217,
        "total": 508_965,
        "write_path": False,
        "done_when": "the module is fenced off from this node",
    },
)


@pytest.mark.parametrize(
    "brief", MEASURED_BRIEFS, ids=[b["row"] for b in MEASURED_BRIEFS]
)
def test_the_measured_briefs_fit_their_lanes_after_the_change(
    tmp_path: Path, brief: dict[str, object]
) -> None:
    """Each recorded brief is re-estimated for its named file and fits the lane.

    The other terms of the recorded estimate are kept as recorded and only the
    named file's contribution is recomputed, so the figure here is the refusal's
    own figure with this node's rule applied to it.
    """
    path = str(brief["path"])
    byte_count = _bytes_for_tokens(int(brief["named_tokens"]))
    assert _estimated_tokens(byte_count) == brief["named_tokens"]
    _seed(tmp_path, [(path, byte_count)])
    node = _node(
        goal=f"the brief names {path} as its subject",
        done_when=str(brief["done_when"]),
        write_paths=[path] if brief["write_path"] else [],
    )

    charge, inputs = _context_file_inputs(tmp_path, node, _authority(tmp_path))
    assert charge == _charge(inputs, path)
    assert charge > 0
    if brief["write_path"]:
        assert charge == brief["named_tokens"]
    else:
        assert charge == REFERENCE_TOKENS

    after_total = int(brief["total"]) - int(brief["named_tokens"]) + charge
    assert after_total <= WINDOW, (brief["row"], after_total)
    if not brief["write_path"]:
        assert after_total < int(brief["total"])
