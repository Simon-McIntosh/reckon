"""The red log's requirements are stated where a worker reads it.

A declared mutation is discharged by the log its run produced: promotion reads
the run's facts from that log — a non-zero terminal ``EXIT=<n>`` record and at
least one failing test id — and refuses a log that carries neither, however it
is worded. A refusal therefore has to say what it wants rather than only that
something is missing, because a worker meets it when the run is over and the
log cannot be rewritten from the mutation's description alone.

The manifest template's gloss beside ``negative_control_log`` states, before
the run, that the log's first line repeats the declared mutation verbatim; that
line is the log's label for a human reader. What admission reads is the run's
facts, and the refusal names the fact it found absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from tests.test_crew_declared_negative_control import (
    REVIEW_WAIVER,
    _promote,
    _write_manifest,
    _write_pointer,
)

pytest_plugins = ("tests.test_crew_declared_negative_control",)

TEST_PATH = "tests/test_guard.py"


# ── The contract a worker is handed ────────────────────────────────────────


def _template_gloss() -> str:
    """The manifest template line a worker fills, as the prompt emits it."""
    node = TaskNode(
        id="nc-header-template",
        goal="the emitted contract states how the red log must name its mutation",
        plan="plan-a",
        section="guard",
        role="implement",
        done_when="the emitted manifest template states the first-line requirement",
        write_paths=[TEST_PATH],
        time_budget="20m",
        manifest_path="/state/runs/nc-header-template/manifest.md",
        negative_control="removing the guard turns its case red",
    )
    prompt = compose_prompt(
        node=node,
        project="proj",
        worktree="/repo/worktrees/nc-header-template",
        working_directory="/repo/worktrees/nc-header-template",
        manifest_path="/state/runs/nc-header-template/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )
    return next(
        line
        for line in prompt.splitlines()
        if line.strip().startswith("negative_control_log:")
    ).strip()


def test_the_emitted_template_states_the_first_line_requirement() -> None:
    """The convention reaches the worker before the refusal does."""
    gloss = _template_gloss()

    # The requirement is the line's position and its exactness, not merely that
    # the log names the mutation somewhere.
    assert "first line" in gloss
    assert "verbatim" in gloss
    assert "declared mutation" in gloss


# ── The refusal a worker meets when the facts are missing ──────────────────


def test_the_refusal_states_the_fact_it_found_absent(
    repository: Path, tmp_path: Path
) -> None:
    """The guarded thing happens: a log whose run left no exit record."""
    run_id = "r-20260919T120000000000-node-a"
    mutation = "removing the guard turns the fixture red"
    red = tmp_path / "other_failure.log"
    red.write_text(
        "FAILED tests/test_other.py - AssertionError\n1 failed, 1 passed\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, red_log=red)
    _write_pointer(
        repository,
        run_id,
        write_paths=[TEST_PATH],
        negative_control=mutation,
        manifest_path=manifest,
    )

    with pytest.raises(crew.CrewError) as refusal:
        _promote(
            repository,
            run_id,
            gate="passed",
            outcome="the guard landed",
            review_waiver=REVIEW_WAIVER,
        )

    message = str(refusal.value)
    # The detail that was already there survives: which declaration, which log.
    assert mutation in message
    assert str(red) in message
    # The refusal states the fact it read and found absent — this log carries
    # no exit record — instead of the first-line wording, which admission no
    # longer compares.
    assert "records no EXIT status" in message
