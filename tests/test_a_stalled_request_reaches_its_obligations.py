"""A request admission has stalled reaches its coordinator as a stalled duty.

Admission records a stall verdict for a request it could launch and did not. That
record reached the drain view and the needs-you rows, but nothing in production
imports the needs-you module any more, so the verdict stopped where it was
written. A coordinator's obligations list built a plain ``run-queued`` row for
every queued request, which says only that the request is waiting its turn — so a
request admission had already failed to take read exactly like one that was
simply queued, and the stuck-in-the-queue experience was unchanged.

These cases pin the route from that record to the coordinator: the stalled
verdict becomes a ``run-stalled`` row naming the age the request has been
admissible and the stage the pass was in, the same row comes out of a fresh
snapshot build as out of the derivation, a request with no verdict keeps its
plain queued row, a later launch verdict clears the stall, the row never holds
the hook's stop, and one build reads the verdict log once however many requests
are queued.

The fleet is the queue fixture's own: a real checkout, a real project mount and
one request per section, so what these cases read is what an admission pass
would have written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import reckon.crew.obligations as obligations_module
from reckon.crew import obligation_snapshot, queue_admission, runs
from reckon.hooks import coordinator_obligations as hook
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

PROJECT = fixtures.PROJECT
SESSION = fixtures.SESSION
chain_repo = fixtures.chain_repo
RUN_ID = "r-n1"
OTHER_RUN_ID = "r-n2"
THIRD_RUN_ID = "r-n3"
#: Eleven minutes: past the ten-minute threshold, which is the wait the lane
#: reported without a reachable record on 28 October.
ADMISSIBLE_SECONDS = 11 * 60
#: The launch stage's duration on the pass that recorded the stall.
STAGE_SECONDS = 12.5
STAGE = "launch"


def _log_path() -> Path:
    return runs.crew_home() / queue_admission.ADMISSION_VERDICT_LOG


def _write_log(records: list[dict]) -> None:
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    _log_path().write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _stall_record(*run_ids: str) -> dict:
    """One pass's log line recording a stall for each named request."""
    return {
        "at": datetime.now(UTC).isoformat(),
        "admissible_age_seconds": float(ADMISSIBLE_SECONDS),
        "pointers": [
            {
                "run_id": run_id,
                "verdict": queue_admission.STALL_VERDICT,
                "stage": STAGE,
                "stage_seconds": STAGE_SECONDS,
                "age_seconds": float(ADMISSIBLE_SECONDS),
            }
            for run_id in run_ids
        ],
    }


def _queue(repository, run_ids: list[str]) -> dict[str, dict]:
    """Queue one request per run id, all on the dependency-free section."""
    nodes = [fixtures._node(run_id.removeprefix("r-"), "s1") for run_id in run_ids]
    stored = queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=fixtures.CONFIG,
        run_ids={node.id: run_id for node, run_id in zip(nodes, run_ids, strict=True)},
    )
    for run_id in run_ids:
        assert stored[run_id]["phase"] == queue_admission.QUEUED_PHASE, stored[run_id]
        pointer = dict(runs.read_pointer(run_id))
        pointer["queued_at"] = (
            datetime.now(UTC) - timedelta(seconds=ADMISSIBLE_SECONDS)
        ).isoformat()
        runs._write_json(runs.pointer_path(run_id), pointer)
    return stored


def _rows(payload: dict, run_id: str) -> list[dict]:
    return [
        row
        for row in payload["obligations"]
        if str(row.get("run_id") or "") == run_id
    ]


def test_a_stalled_request_is_a_stalled_row_in_obligations(chain_repo) -> None:
    """The recorded stall reaches the coordinator as a stalled row, not a queued one."""
    _queue(chain_repo, [RUN_ID])
    _write_log([_stall_record(RUN_ID)])

    payload = obligations_module.obligations(PROJECT, SESSION)

    rows = _rows(payload, RUN_ID)
    assert len(rows) == 1, payload["obligations"]
    row = rows[0]
    assert row["kind"] == obligations_module.STALLED_RUN_DUTY_KIND, row
    # The age is the pass's own admissible age, and the stage is the one the
    # pass was in with that stage's duration on it.
    assert row["age_seconds"] == ADMISSIBLE_SECONDS, row
    assert row["stage"] == STAGE, row
    assert row["stage_seconds"] == STAGE_SECONDS, row
    assert f"--run {RUN_ID}" in row["next_command"], row
    assert "stalled at the launch stage" in row["next_command"], row


def test_a_fresh_snapshot_build_carries_the_stalled_row(chain_repo) -> None:
    """A snapshot built from the same files reports the same stalled row."""
    _queue(chain_repo, [RUN_ID])
    _write_log([_stall_record(RUN_ID)])

    state = obligation_snapshot.fleet_state(PROJECT)
    payload = obligation_snapshot.payload_for(state, SESSION)

    rows = _rows(payload, RUN_ID)
    assert len(rows) == 1, payload["obligations"]
    assert rows[0]["kind"] == obligations_module.STALLED_RUN_DUTY_KIND, rows[0]
    assert rows[0]["age_seconds"] == ADMISSIBLE_SECONDS, rows[0]
    assert rows[0]["stage"] == STAGE, rows[0]
    assert rows[0]["stage_seconds"] == STAGE_SECONDS, rows[0]


def test_a_request_without_a_stall_verdict_keeps_its_queued_row(chain_repo) -> None:
    """A request that is simply waiting its turn still reads as queued."""
    _queue(chain_repo, [RUN_ID])
    # A pass that looked at the request and recorded an ordinary wait, which is
    # everything a request that is merely queued ever gets.
    _write_log(
        [
            {
                "at": datetime.now(UTC).isoformat(),
                "pointers": [{"run_id": RUN_ID, "verdict": "held-on-claim"}],
            }
        ]
    )

    payload = obligations_module.obligations(PROJECT, SESSION)

    rows = _rows(payload, RUN_ID)
    assert len(rows) == 1, payload["obligations"]
    assert rows[0]["kind"] == obligations_module.QUEUED_RUN_DUTY_KIND, rows[0]
    assert "stage" not in rows[0], rows[0]


def test_a_later_launch_verdict_clears_the_stalled_row(chain_repo) -> None:
    """The pass that launches the request replaces the stall it recorded."""
    _queue(chain_repo, [RUN_ID])
    _write_log([_stall_record(RUN_ID)])
    assert _rows(obligations_module.obligations(PROJECT, SESSION), RUN_ID)[0][
        "kind"
    ] == obligations_module.STALLED_RUN_DUTY_KIND

    with _log_path().open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "pointers": [{"run_id": RUN_ID, "verdict": "launched"}],
                }
            )
            + "\n"
        )

    rows = _rows(obligations_module.obligations(PROJECT, SESSION), RUN_ID)
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == obligations_module.QUEUED_RUN_DUTY_KIND, rows[0]


def test_a_stalled_row_does_not_hold_the_hook_stop(chain_repo) -> None:
    """The row is listed, and it never keeps the coordinator's turn open."""
    _queue(chain_repo, [RUN_ID])
    _write_log([_stall_record(RUN_ID)])

    payload = obligations_module.obligations(PROJECT, SESSION)
    stalled = _rows(payload, RUN_ID)
    assert stalled and stalled[0]["kind"] == obligations_module.STALLED_RUN_DUTY_KIND

    assert hook._blocking_items(payload["obligations"]) == []
    assert "run-stalled" in hook._NON_BLOCKING_KINDS
    assert obligations_module.STALLED_RUN_DUTY_KIND in (
        obligations_module.NON_BLOCKING_DUTY_KINDS
    )


def test_one_obligations_build_reads_the_verdict_log_once(
    chain_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three queued requests are told apart by one reading of the log."""
    _queue(chain_repo, [RUN_ID, OTHER_RUN_ID, THIRD_RUN_ID])
    _write_log([_stall_record(RUN_ID, OTHER_RUN_ID)])

    reads: list[int] = []
    original = obligations_module.admission_verdicts

    def counted() -> dict:
        reads.append(1)
        return original()

    monkeypatch.setattr(obligations_module, "admission_verdicts", counted)

    payload = obligations_module.obligations(PROJECT, SESSION)

    kinds = {
        run_id: [row["kind"] for row in _rows(payload, run_id)]
        for run_id in (RUN_ID, OTHER_RUN_ID, THIRD_RUN_ID)
    }
    assert kinds == {
        RUN_ID: [obligations_module.STALLED_RUN_DUTY_KIND],
        OTHER_RUN_ID: [obligations_module.STALLED_RUN_DUTY_KIND],
        THIRD_RUN_ID: [obligations_module.QUEUED_RUN_DUTY_KIND],
    }, kinds
    assert len(reads) == 1, f"the build read the verdict log {len(reads)} times"