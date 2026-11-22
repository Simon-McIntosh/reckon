"""A reflex resume carries the compute partition its dispatch named.

A run's done-when may name the partition its heavy gate runs on, and the
dispatch prompt names that partition in the gate calls it composes. A resume
composed by the review or repair reflex carries the advice alone, and the
advice names no partition: the resumed worker then runs the gate wherever the
resume landed, which is how a repair round put ``pytest tests/unit/python`` on
the fleet node and raised a fleet-test-processes obligation.

Both halves are driven through the same door a reflex resume takes,
``resumption._resume``, with a launcher that reads back the prompt file the
worker would be handed. A run whose done-when names a partition must read that
partition and the instruction to run the gate there; a run whose done-when
names none must read neither, because a partition the dispatch never chose is
not the dispatch's instruction to carry.

The declared negative control is the base revision's advice, which carries no
compute instruction at all, so the first case must fail with the partition
absent from the resume prompt.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import resumption, runs
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    CONFIG,
    _stopped_pointer,
    crew_home,
    operator_home,
    repo,
)

pytestmark = pytest.mark.arms_watch_producer

RUN_ID = "r-reflex-resume-partition"
PARTITION = "all_debug"
PARTITION_HEADER = "FENCE — COMPUTE PARTITION"
GATE_THERE = "Run the gate there"
REFLEX_ADVICE = "A repair round found one blocking finding (f1). Answer it in this run."


def _record_dispatched_with(tmp_path: Path, repo_path: Path, done_when: str) -> dict:
    """A stopped run whose node names ``done_when`` for its gate."""
    record = _stopped_pointer(tmp_path, repo_path, RUN_ID, backend="alpha")
    record["node"]["done_when"] = done_when
    crew._write_json(runs.pointer_path(RUN_ID), record)
    return record


def _resume_prompt(record: dict) -> str:
    """The prompt file the resumed worker is handed."""
    written: list[str] = []

    def launcher(plan, *, log_path, stderr_path, prompt_path) -> int:
        written.append(Path(prompt_path).read_text(encoding="utf-8"))
        return os.getpid()

    resumption._resume(
        RUN_ID, record, config=CONFIG, launcher=launcher, advice=REFLEX_ADVICE
    )
    assert written, "the resume launched no worker"
    return written[0]


def test_a_resume_carries_the_partition_its_dispatch_named(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The positive half: the named partition and the gate instruction ride it."""
    repo_path = request.getfixturevalue("repo")
    record = _record_dispatched_with(
        tmp_path,
        repo_path,
        f"Done when the gate passes on the {PARTITION} partition.",
    )

    prompt = _resume_prompt(record)

    assert PARTITION in prompt, "the resume prompt names no partition"
    assert PARTITION_HEADER in prompt, "the resume prompt carries no compute fence"
    assert GATE_THERE in prompt, "the resume prompt gives no gate instruction"


def test_a_resume_of_a_dispatch_that_named_none_carries_none(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The negative half: no partition is invented for a dispatch that named none."""
    repo_path = request.getfixturevalue("repo")
    record = _record_dispatched_with(
        tmp_path, repo_path, "Done when each arm's log carries its closing EXIT= line."
    )

    prompt = _resume_prompt(record)

    assert PARTITION_HEADER not in prompt, "an unnamed partition was invented"
    assert PARTITION not in prompt, "a partition appears that no dispatch named"
