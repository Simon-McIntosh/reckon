"""The ship-skill landing clauses mirror the runtime prompt's own wording.

Four clauses once told a worker never to write plan or index state. Each now
carries the narrowed prohibition plus the positive requirement: the worker
appends its landing record to its own section of the plan and its evidence
anchor to the cumulative evidence record, both in its worktree and both in its
final commit, and never touches shared project index, sprint state, another
plan, or the version and modified meta lines.

The measure is not that the prohibitions are gone but that the skill and the
prompt say the same thing. The expected sentences below are therefore derived
at test time from the prompt's own strings — the constant and a freshly
composed prompt — never written here as literals. A check pinned to a literal
copy reproduces the defect being fixed: two copies of one rule drift silently
and the test keeps passing. Here a prompt rewording that is not mirrored into
the skills fails on the next run, because the extracted expectation changes
while the skill still carries the old sentence, or the locator fails to be
found at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.prompts import (
    CLOSURE_AUTHORITY_CONTRACT,
    PLAN_LANDING_CONTRACT,
    compose_prompt,
)

ROOT = Path(__file__).resolve().parents[1]

SKILL_FILES = (
    ROOT / "skills" / "reckon-build" / "SKILL.md",
    ROOT / "skills" / "reckon-build" / "references" / "worker-protocol.md",
    ROOT / "skills" / "reckon-build" / "references" / "sprint-orchestration.md",
)

# The four phrasings this plan retires from the ship skills. The done-when
# states their count as a digit before and after; the leaf assertion here keeps
# a reappearance from passing silently.
RETIRED_PHRASINGS = (
    "they never write shared plan state",
    "Do not edit reckon plan or index state",
    "Do not edit Reckon plan/index state",
    "write shared plan or index state",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompt() -> str:
    """A composed runtime prompt for an implement node, embedding the landing
    contract and the worktree rules the skills must mirror."""
    return compose_prompt(
        node=TaskNode(
            id="parity-node",
            goal="the ship skills carry the prompt's own landing contract wording",
            plan="worker-authors-its-own-record",
            section="",
            role="implement",
            done_when="each skill file keeps the prompt's landing clauses verbatim",
            write_paths=["skills/reckon-build/"],
            time_budget="20m",
        ),
        project="reckon",
        worktree="/repo/worktrees/parity-run",
        working_directory="/repo/worktrees/parity-run",
        manifest_path="/state/runs/parity-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _slice_between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    finish = text.index(end, begin) + len(end)
    return text[begin:finish]


def _landing_requirement() -> str:
    """The positive sentence, sliced from the prompt's own constant."""
    return _flat(
        _slice_between(
            _flat(PLAN_LANDING_CONTRACT),
            "Append your landing record to your own section",
            "go into your final commit.",
        )
    )


def _meta_line_ban() -> str:
    """The version/meta-line sentence, sliced from the prompt's own constant."""
    return _flat(
        _slice_between(
            _flat(PLAN_LANDING_CONTRACT),
            "Do not edit the plan-version or plan-modified",
            "every merge conflict there.",
        )
    )


def _narrowed_prohibition() -> str:
    """The narrowed shared-state ban, sliced from the composed prompt's own
    worktree rules rather than from a second copy here."""
    return _flat(
        _slice_between(
            _flat(_prompt()),
            "Never mutate the shared project index",
            "you are landing against.",
        )
    )


# The coordinator-side closure limits travel in two skill files, not three. The
# worker-half sentences above reach every one of SKILL_FILES, but the two limits
# are stated only where a coordinator is told how a section closes, which is
# SKILL.md and references/sprint-orchestration.md; references/worker-protocol.md
# carries the worker's landing requirement and not the coordinator's closure
# authority. An expectation derived from the prompt is therefore checked against
# exactly the files that state the sentence, so a prompt rewording that is not
# mirrored into either one fails on the next run.
LIMIT_SKILL_FILES = (
    ROOT / "skills" / "reckon-build" / "SKILL.md",
    ROOT / "skills" / "reckon-build" / "references" / "sprint-orchestration.md",
)


def _closure_authority_limit() -> str:
    """The two limits with their see-based reason, sliced from the prompt's own
    constant so a skill sentence must match the prompt's words or the test
    fails, exactly as with the landing requirement above."""
    return _flat(
        _slice_between(
            _flat(CLOSURE_AUTHORITY_CONTRACT),
            "must not resolve its own driving followup",
            "not whether the section closed.",
        )
    )


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_retired_phrasings_are_absent(path: Path) -> None:
    text = _flat(path.read_text())
    for phrasing in RETIRED_PHRASINGS:
        assert _flat(phrasing) not in text, f"{path.name} still carries {phrasing!r}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_paths_carry_the_landing_requirement_from_the_prompt(path: Path) -> None:
    assert _landing_requirement() in _flat(path.read_text()), path


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_paths_carry_the_narrowed_prohibition_from_the_prompt(path: Path) -> None:
    assert _narrowed_prohibition() in _flat(path.read_text()), path


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_paths_carry_the_meta_line_ban_from_the_prompt(path: Path) -> None:
    assert _meta_line_ban() in _flat(path.read_text()), path


@pytest.mark.parametrize("path", LIMIT_SKILL_FILES, ids=lambda p: p.name)
def test_paths_carry_the_closure_limits_from_the_prompt(path: Path) -> None:
    assert _closure_authority_limit() in _flat(path.read_text()), path


def test_the_extracted_sentences_are_the_prompt_s_own_words() -> None:
    """The locators must still resolve for the derivations to mean anything; a
    prompt reword of an anchor fails loudly here rather than coercing a pass."""
    prompt = _flat(_prompt())
    assert _flat(PLAN_LANDING_CONTRACT) in prompt
    assert _landing_requirement() in prompt
    assert _meta_line_ban() in prompt
    assert _narrowed_prohibition() in prompt
    assert _closure_authority_limit() in prompt
