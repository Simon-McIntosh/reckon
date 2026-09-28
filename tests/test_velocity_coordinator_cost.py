"""Coordinator cost per landed node, driven by synthesised transcripts.

The census this section ports reads the live transcript store under
``~/.claude/projects`` and the committed run ledger of five repositories, so
parity against the review's pinned window is a recorded measurement rather than
a test: a test must not read or write state outside the repository under test.
What this module verifies here is the accounting on transcripts and run rows it
builds itself under ``tmp_path``.

The expectations are derived from the fixture the code sees, not echoed from
the code's output. The cache-read share is exercised on its own: if it were
folded into the uncached input figure the separation test would move from a
positive total to zero, so the assertion is load-bearing rather than decorative.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import velocity

# An instant inside the pinned window, so a fixture record needs no clock maths.
AT = "2026-09-20T12:00:00Z"
OUTSIDE = "2026-09-30T12:00:00Z"
SESSION = "11111111-1111-1111-1111-111111111111"
MISSING = "22222222-2222-2222-2222-222222222222"


def _assistant(message_id: str, at: str, **usage) -> dict:
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {"id": message_id, "usage": usage},
    }


def _write_transcript(root: Path, session: str, records: list[dict]) -> None:
    directory = root / "a-project"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{session}.jsonl"
    with target.open("w") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def _runs() -> list[dict]:
    return [
        {
            "run_id": "r-1",
            "project": "sample",
            "coordinator": {"runtime_session_id": SESSION},
        },
        {
            "run_id": "r-2",
            "project": "sample",
            "coordinator": {"runtime_session_id": SESSION},
        },
        {
            "run_id": "r-3",
            "project": "sample",
            "coordinator": {"session_id": "unlabelled-human"},
        },
        {
            "run_id": "r-4",
            "project": "sample",
            "coordinator": {"runtime_session_id": MISSING},
        },
    ]


def _transcript(root: Path) -> None:
    _write_transcript(
        root,
        SESSION,
        [
            {"type": "user", "timestamp": AT, "message": {"content": "go"}},
            _assistant(
                "msg-a",
                AT,
                input_tokens=100,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=1000,
                output_tokens=50,
            ),
            # A streaming re-emission of msg-a with a larger cache-read count:
            # the maximum per key is kept, never summed on top.
            _assistant(
                "msg-a",
                AT,
                input_tokens=100,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=1100,
                output_tokens=50,
            ),
            _assistant(
                "msg-b",
                AT,
                input_tokens=200,
                cache_creation_input_tokens=20,
                cache_read_input_tokens=2000,
                output_tokens=60,
            ),
            # Excluded: outside the window, and a sidechain record.
            _assistant("msg-out", OUTSIDE, input_tokens=9999, output_tokens=9999),
            {
                "type": "assistant",
                "timestamp": AT,
                "isSidechain": True,
                "message": {
                    "id": "msg-side",
                    "usage": {"input_tokens": 9999, "output_tokens": 9999},
                },
            },
        ],
    )
    # A line that is not JSON at all must be skipped, not crash the reader.
    with (root / "a-project" / f"{SESSION}.jsonl").open("a") as stream:
        stream.write("{not json\n")


@pytest.fixture()
def cost(tmp_path: Path) -> dict:
    root = tmp_path / "projects"
    _transcript(root)
    return velocity.coordinator_cost(_runs(), transcript_root=root)


def _session(cost: dict, session: str) -> dict:
    return next(row for row in cost["sessions"] if row["session_id"] == session)


def test_repeated_response_and_out_of_window_records_change_nothing(cost: dict):
    row = _session(cost, SESSION)
    # Two distinct message ids survive; the repeated msg-a counts once.
    assert row["transcript_status"] == "captured"
    assert row["assistant_responses"] == 2


def test_cache_read_tokens_are_reported_separately(cost: dict):
    tokens = _session(cost, SESSION)["tokens"]
    # Copied ledgers: uncached 100+200, creation 10+20, cache read 1100+2000.
    assert tokens["uncached_input_tokens"] == 300
    assert tokens["cache_creation_input_tokens"] == 30
    assert tokens["cache_read_input_tokens"] == 3100
    assert tokens["output_tokens"] == 110
    assert tokens["input_tokens"] == 300 + 30 + 3100
    # The separation is the point: a fold into uncached leaves this at zero.
    assert tokens["cache_read_input_tokens"] > 0


def test_each_figure_is_divided_by_landed_nodes(cost: dict):
    row = _session(cost, SESSION)
    assert row["landed_nodes"] == 2
    assert row["assistant_responses_per_landed_node"]["value"] == pytest.approx(1.0)
    per_node = row["tokens_per_landed_node"]
    assert per_node["cache_read_input_tokens"]["value"] == pytest.approx(1550.0)
    assert per_node["input_tokens"]["value"] == pytest.approx(1715.0)
    assert per_node["output_tokens"]["value"] == pytest.approx(55.0)


def test_a_run_without_coordinator_identity_enters_an_unattributed_bucket(cost: dict):
    row = _session(cost, "unattributed:sample:unlabelled-human")
    assert row["attribution"] == "unattributed"
    assert row["landed_nodes"] == 1
    assert row["tokens"] is None
    assert cost["totals"]["unattributed_landed_nodes"] == 1


def test_a_session_without_a_transcript_reports_null_not_zero(cost: dict):
    row = _session(cost, MISSING)
    assert row["transcript_status"] == "missing"
    assert row["assistant_responses"] is None
    assert row["tokens"] is None
    assert row["tokens_per_landed_node"] is None


def test_measure_reports_the_section_without_reading_a_transcript_store(tmp_path: Path):
    # With no transcript root the section still reports landed counts, and the
    # committed suite never reaches into the live home directory.
    snapshot = {
        "window": [velocity.START, velocity.END],
        "august_baseline": None,
        "projects": [
            {
                "project": "sample",
                "primary_branch": "main",
                "head": "h",
                "base": "b",
                "ledger_sha256": "x",
                "per_run_files": 0,
                "recovered_from_sqlite": [],
                "promotion_ids_without_record": [],
                "commits": [],
                "runs": [
                    {
                        "run_id": "r-1",
                        "project": "sample",
                        "node": "n",
                        "plan": "p",
                        "role": "implement",
                        "gate": "passed",
                        "backend": "claude",
                        "dispatched_at": velocity.START,
                        "completed_at": velocity.END,
                        "attempt": 1,
                        "lineage": {},
                        "resolved_commits": [],
                        "commits": [],
                        "coordinator": {"runtime_session_id": SESSION},
                        "promotion_commits": [
                            {
                                "sha": "deadbeef",
                                "epoch": velocity.stamp(AT),
                                "landing_sha": "deadbeef",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    result = velocity.measure(
        snapshot, projects={"sample": "main"}, transcript_root=None
    )
    section = result["coordinator_cost"]
    assert section["totals"]["landed_nodes"] == 1
    assert section["sessions"][0]["transcript_status"] == "unread"
    assert section["sessions"][0]["tokens"] is None