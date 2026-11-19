"""The red log's requirements are stated where a worker reads it.

A declared mutation is discharged by the log its run produced: promotion reads
the run's facts from that log — a non-zero terminal ``EXIT=<n>`` record and at
least one failing test id — and refuses a log that carries neither, however it
is worded. A refusal therefore has to say what it wants rather than only that
something is missing, because a worker meets it when the run is over and the
log cannot be rewritten from the mutation's description alone.

The manifest template's gloss beside ``negative_control_log`` states, before
the run, the rule admission applies: the log is judged on the facts its run
recorded — a non-zero exit line and a failing test id the head arm does not
fail — never on its wording. The refusal names the fact it found absent.
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


def test_the_emitted_template_states_the_facts_admission_rule() -> None:
    """The rule reaches the worker before the refusal does."""
    gloss = _template_gloss()

    # The facts admission reads, stated where the worker writes the path in:
    # a non-zero exit line and a failing test id the head arm does not fail.
    assert "`EXIT=`" in gloss
    assert "failing test id the head arm does not fail" in gloss
    assert "never on the log's wording" in gloss
    assert "verbatim" not in gloss
    assert "first line" not in gloss


# ── The refusal a worker meets when the facts are missing ──────────────────


def test_the_refusal_states_the_fact_it_found_absent(
    repository: Path, tmp_path: Path
) -> None:
    """The guarded thing happens: a log whose run recorded no failure anywhere."""
    run_id = "r-20260919T120000000000-node-a"
    mutation = "removing the guard turns the fixture red"
    red = tmp_path / "other_failure.log"
    # Neither record a run could have left: no capture's EXIT line, and no
    # runner output stating that anything failed. Wording alone is what is
    # left, and wording is the one thing admission does not read.
    red.write_text("1 passed in 1.21s\n", encoding="utf-8")
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
    # The refusal states the fact it read and found absent — this log states
    # no failure in either record a run writes — instead of the first-line
    # wording, which admission no longer compares.
    assert "reports no failure" in message
    assert "first line" not in message
    assert "verbatim" not in message


# ── The rule the gate keeps: a runner's own record is the failure record ───


def test_a_runner_recorded_failure_admits_the_control(
    repository: Path, tmp_path: Path
) -> None:
    """A log with no EXIT line is admitted on the runner's own FAILED record."""
    run_id = "r-20260919T120100000000-node-a"
    mutation = "removing the guard turns the fixture red"
    reddened = "tests/test_guard.py::test_the_guard_refuses"
    red = tmp_path / "guard_removed.log"
    red.write_text(
        f"FAILED {reddened} - AssertionError: the guard admitted the call\n"
        "1 failed, 4 passed in 0.31s\n",
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

    promoted = _promote(
        repository,
        run_id,
        gate="passed",
        outcome="the guard landed",
        review_waiver=REVIEW_WAIVER,
    )

    control = promoted["negative_control"]
    assert control["verdict"] == "matched"
    assert control["declaration"] == mutation
    # No capture line was written, and the failure came from the runner.
    assert control["control_exit_status"] is None
    assert control["control_run_failed"] is True
    assert control["added_failure_ids"] == [reddened]
