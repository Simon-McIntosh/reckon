"""A result a figure shows more clearly than prose leaves a figure, not a report.

The composed worker contract carried two clauses about evidence that met as a
contradiction: one withheld a figure unless the rule's done-when named the
artifact, the other asked for the figure wherever a figure reads better than
prose. A worker obeyed the withholding clause and wrote a fragment of prose,
which is what the fleet did — hundreds of landings and a handful of figures.

The rule is now stated once, in one producer that composes it from the node's
own done-when, and every landing contract reads that one producer. A done-when
states a measure by naming a figure artifact or by stating a measured quantity,
so a quantitative done-when that never says "figure" still asks for one. This
module asserts the four facts the plan's done-when names, and one more: that the
rule's text has one home, so editing it there changes every composed contract
rather than one of several copies.

The expected sentences below are written out rather than imported from the
producer: an assertion that compares a constant with the same constant the
producer inserted cannot tell whether the prompt asks a worker for a figure, and
the fixtures state a measure without reusing the classifier's own words.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import reckon.crew.prompts as prompts_mod
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = REPO_ROOT / "skills/reckon-build/references/worker-protocol.md"

# A done-when naming a figure artifact, and one stating a measured quantity
# without the word "figure": the two ways a done-when states a measure.
FIGURE_DONE_WHEN = (
    "the measure's figure shows the retained share and names its topic directory"
)
MEASURED_DONE_WHEN = "the gate cuts runtime by 30 percent"
RENAME_DONE_WHEN = "rename the helper without changing its behaviour, and offer none"

# What the composed contract must say on each side, in its own words.
EXPECTED_COMMON = (
    "Keep evidence prose to the landing line, and write an evidence fragment "
    "only for a negative result; the landing fragment is always permitted at "
    "its scope path."
)
EXPECTED_ANNOUNCED = (
    "Your done-when states a measure, so draw the figure that shows it — or an "
    "animation where the result is a sequence in time — wherever the result is "
    "one a figure shows more clearly than prose, and name it on the manifest's "
    "`figures:` line."
)
EXPECTED_WITHHELD = (
    "Your done-when states no measure, so this node has no figure to draw."
)

# The directory and project-absolute path each carrier grants. The plan carrier
# names the node's own directory; the brief carrier names the plan-wide topic a
# brief run writes into, leaf path included.
PLAN_DIRECTORY = "docs/figures/<plan>/<node-id>/"
PLAN_SRC = "src /<project>/figures/<plan>/<node-id>/"
BRIEF_DIRECTORY = "docs/figures/<topic>/"
BRIEF_SRC = "src /<project>/figures/<topic>/"

BRIEF_TEXT = "Report what you found for the next reader."

# A clause that asks for a figure, and one that withholds it, in the vocabulary
# either revision of the contract uses. The pair is what the contract must never
# carry at once, so the patterns are deliberately broader than today's wording.
ASK_PATTERN = re.compile(
    r"\b(?:use a figure|draw the figure|draw a figure)\b", re.IGNORECASE
)
WITHHOLD_PATTERN = re.compile(
    r"(?:no figure and no evidence prose|permits no other figure"
    r"|writes no figure|no figure to draw)"
)


def _flat(text: str) -> str:
    """Collapse manual line-wrapping so a phrase is found regardless of where
    the composer or the reference file broke the line."""
    return " ".join(text.split())


def _node(*, done_when: str, brief: bool, role: str = "implement") -> TaskNode:
    return TaskNode(
        id="figure-node",
        goal="state what a result owes",
        plan="" if brief else "plan-a",
        section="" if brief else "s7",
        role=role,
        done_when=done_when,
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )


def _compose(done_when: str = FIGURE_DONE_WHEN, *, brief: bool = False) -> str:
    return compose_prompt(
        node=_node(done_when=done_when, brief=brief),
        project="proj",
        worktree="/repo/worktrees/figure-node",
        working_directory="/repo/worktrees/figure-node",
        manifest_path="/state/runs/figure-node/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        can_write_worktree=True,
        brief=BRIEF_TEXT if brief else "",
    )


# ── A node whose done-when states a measure is asked for its figure ─────────


@pytest.mark.parametrize(
    "done_when", [FIGURE_DONE_WHEN, MEASURED_DONE_WHEN], ids=["named", "measured"]
)
def test_a_drawable_done_when_asks_for_the_figure_and_names_its_directory(
    done_when: str,
) -> None:
    flat = _flat(_compose(done_when))

    assert EXPECTED_COMMON in flat
    assert EXPECTED_ANNOUNCED in flat
    assert PLAN_DIRECTORY in flat
    assert PLAN_SRC in flat


def test_a_measured_done_when_is_never_told_it_states_no_measure() -> None:
    flat = _flat(_compose(MEASURED_DONE_WHEN))

    assert EXPECTED_WITHHELD not in flat


def test_a_drawable_brief_asks_for_the_figure_and_names_the_topic_directory():
    flat = _flat(_compose(MEASURED_DONE_WHEN, brief=True))

    assert EXPECTED_ANNOUNCED in flat
    assert BRIEF_DIRECTORY in flat
    assert BRIEF_SRC in flat


# ── A node whose done-when states no measure is asked for none ──────────────


def test_a_rename_done_when_asks_for_no_figure():
    for brief in (False, True):
        flat = _flat(_compose(RENAME_DONE_WHEN, brief=brief))

        assert EXPECTED_ANNOUNCED not in flat
        assert EXPECTED_WITHHELD in flat


# ── No contract both withholds a figure and asks for one ────────────────────


@pytest.mark.parametrize("brief", [False, True], ids=["plan", "brief"])
@pytest.mark.parametrize(
    "done_when",
    [FIGURE_DONE_WHEN, MEASURED_DONE_WHEN, RENAME_DONE_WHEN],
    ids=["named", "measured", "rename"],
)
def test_no_contract_both_withholds_a_figure_and_asks_for_one(
    brief: bool, done_when: str
) -> None:
    flat = _flat(_compose(done_when, brief=brief))

    asks = bool(ASK_PATTERN.search(flat))
    withholds = bool(WITHHOLD_PATTERN.search(flat))
    assert not (asks and withholds), flat


# ── The manifest template carries the `figures:` key the rule names ─────────


def test_the_manifest_template_names_the_figures_line() -> None:
    flat = _flat(_compose())

    assert (
        "figures: none | <each figure path the run commits, under "
        "docs/figures/<plan>/" in flat
    )


# ── The reference states the same rule in the same words ────────────────────


def test_the_worker_protocol_states_the_same_rule_in_the_same_words():
    flat = _flat(PROTOCOL.read_text(encoding="utf-8"))

    assert _flat(EXPECTED_COMMON) in flat
    assert _flat(EXPECTED_ANNOUNCED) in flat
    assert "has no figure to draw" in flat


# ── The rule has one home, so editing it there changes every contract ───────


def test_changing_the_rule_changes_every_composed_contract(monkeypatch):
    before = {
        (brief, done_when): _flat(_compose(done_when, brief=brief))
        for brief in (False, True)
        for done_when in (FIGURE_DONE_WHEN, MEASURED_DONE_WHEN, RENAME_DONE_WHEN)
    }
    marker = "Every contract reads this one sentence about what a result owes."

    monkeypatch.setattr(
        prompts_mod,
        "EVIDENCE_FIGURE_RULE_COMMON",
        prompts_mod.EVIDENCE_FIGURE_RULE_COMMON + "  " + marker + "\n",
    )

    for (brief, done_when), originally in before.items():
        assert marker not in originally
        assert marker in _flat(_compose(done_when, brief=brief))
