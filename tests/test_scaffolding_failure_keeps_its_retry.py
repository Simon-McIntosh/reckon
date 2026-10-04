"""A worker's own failed probes do not spend attempts on the work."""

from __future__ import annotations

import json

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from reckon.crew.reports import parse_manifest


@pytest.mark.parametrize(
    ("attribution", "counted", "fence_open"),
    [
        (
            {
                "probe-one": "scaffolding: helper called with a stale signature",
                "probe-two": "scaffolding: measurement script used a missing argument",
            },
            0,
            True,
        ),
        (None, 2, False),
        ({"probe-one": "scaffolding: helper called with a stale signature"}, 1, True),
        (
            {
                "probe-one": "scaffolding: ",
                "probe-two": "code-under-test: assertion failed",
            },
            2,
            False,
        ),
    ],
)
def test_the_manifest_reader_counts_only_substantive_or_unattributed_failures(
    attribution: dict[str, str] | None, counted: int, fence_open: bool
) -> None:
    manifest = parse_manifest(
        "status: in-progress\n"
        'retry_failures: ["probe-one", "probe-two"]\n'
        + (
            "failure_attribution: " + json.dumps(attribution)
            if attribution is not None
            else ""
        )
    )
    assert manifest["retry_counted_failures"] == counted
    assert (counted < 2) is fence_open


def test_an_absent_failure_record_is_unknown() -> None:
    assert parse_manifest("status: in-progress\n")["retry_counted_failures"] is None


def test_the_composed_brief_tells_the_worker_which_failures_spend_a_retry() -> None:
    node = TaskNode(
        id="retry-example",
        goal="measure the work",
        plan="example",
        section="",
        role="implement",
        done_when="report the measurement",
        write_paths=[],
        time_budget="20m",
    )
    prompt = compose_prompt(
        node=node,
        project="example",
        worktree="/example",
        working_directory="/example",
        manifest_path="/example/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        brief="Measure the work.",
    )
    flat = " ".join(prompt.split())
    assert "retry_failures: <failure ids for repeated runs of the same command" in flat
    assert (
        'failure_attribution: <inline JSON {failure_id: "scaffolding: cause"}' in flat
    )
    assert "same command has 2 counted failures" in flat
    assert "without reaching code under test" in flat
    assert "A missing or unclear attribution counts" in flat
    assert "a failure in code under test counts" in flat
