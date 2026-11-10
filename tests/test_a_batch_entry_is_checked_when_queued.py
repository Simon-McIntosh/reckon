"""A batch entry is judged by the node contract when it is queued.

The node contract is one function, ``node.validate_node``, and both dispatch
paths call it: ``dispatch_plan.plan_dispatch`` at its verdict and
``dispatch.queue_requests`` for each entry before a pointer is written. This
file pins what that means for a batch — an entry a single dispatch would refuse
is refused in its own batch row with no request stored, while its valid
siblings are stored — and the single path reports the same property, because a
contract two paths judge differently is two contracts.

The checkout, plan and crew-home fixtures come from
``tests.test_every_path_meets_the_node_contract``, which drives the same two
paths for the contract's other properties; one fixture serves both files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_every_path_meets_the_node_contract import (
    NEGATIVE_CONTROL_DETAIL,
    TEST_FILE,
    _dry_run,
    _dry_run_details,
    _entry,
    _pointers,
    _queue,
)
from tests.test_every_path_meets_the_node_contract import (
    crew_home as crew_home,  # noqa: PLC0414 - the shared fixture
)
from tests.test_every_path_meets_the_node_contract import (
    repository as repository,  # noqa: PLC0414 - the shared fixture
)

#: Each case drives queueing through the real reckon home and a real checkout,
#: so a fixture that blocks would stall an arm rather than fail it. The bound
#: turns a hang into a named failure the arm reports.
pytestmark = pytest.mark.timeout(120)


def _test_writing_entry(**overrides: object) -> dict:
    """An entry whose write paths include a test file and declare no mutation."""
    return _entry(node="no-negative-control", write_paths=[TEST_FILE], **overrides)


def test_a_batch_stores_the_valid_entry_and_refuses_the_invalid_one(
    crew_home: Path, repository: Path
) -> None:
    starting = _pointers()

    result = _queue(repository, [_test_writing_entry(), _entry(node="valid-entry")])

    assert result["refused"] == 1, result
    assert result["queued"] == 1, result
    refused_row, stored_row = result["results"]
    assert refused_row["status"] == "refused", refused_row
    assert NEGATIVE_CONTROL_DETAIL in str(refused_row["reason"]), refused_row
    assert stored_row["status"] in {"queued", "awaiting-upstream"}, stored_row
    # Exactly one pointer was written, and it belongs to the stored entry: the
    # refused entry never becomes a request, so no launch can spend a queue
    # position on it and no obligation reports it as waiting.
    after = _pointers()
    assert len(after) == len(starting) + 1, after
    assert f"{stored_row['run_id']}.json" in after


def test_a_batch_of_valid_entries_is_queued_whole(
    crew_home: Path, repository: Path
) -> None:
    starting = _pointers()

    result = _queue(
        repository,
        [_entry(node="first-entry"), _entry(node="second-entry")],
    )

    assert result["refused"] == 0, result
    assert result["queued"] == 2, result
    stored = [row for row in result["results"] if row.get("run_id")]
    assert len(stored) == 2, stored
    assert len({row["run_id"] for row in stored}) == 2, stored
    after = _pointers()
    assert len(after) == len(starting) + 2, after
    for row in stored:
        assert f"{row['run_id']}.json" in after, row


def test_a_single_dispatch_refuses_the_same_node_for_the_same_property(
    crew_home: Path, repository: Path
) -> None:
    entry = _test_writing_entry()

    payload = _dry_run(entry, repository)

    assert (payload.get("validation") or {}).get("ok") is False, payload
    assert NEGATIVE_CONTROL_DETAIL in _dry_run_details(payload), payload
