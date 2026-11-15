"""One plan's run records read without decoding another plan's history.

The ledger's own read already selected by plan, but it decoded every run
document first and filtered the decoded rows afterwards, so one plan's read
decoded the whole store's payloads. The selection now goes into the index's
query — the one owner of decoding and filtering, which already pushes the
activity bound the same way.

This test builds a store of 2,000 runs across 50 plans, reads one plan through
``ledger.runs``, and counts the payloads that read decodes: none of another
plan's, with the unfiltered read over the same store as the positive control
that the counting instrument sees every plan present. The rows are also the
ones the unfiltered read returns filtered afterwards, in the same order, so the
filter moved the cost and not the answer.

At ``BASE_REVISION`` the decode-count case decodes every plan's documents; the
negative control recorded with this node's manifest is that run.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest import mock

import pytest

from reckon import ledger

PROJECT = "demo"
#: The done-when's population: 2,000 runs across 50 plans.
PLAN_COUNT = 50
RUNS_PER_PLAN = 40
#: The plan the measured read names; every other plan is what it must not cost.
SELECTED = "plan-07"
#: The revision this file was written after. The negative control runs it there.
BASE_REVISION = "19b28c494ed17c302a6b6be1d99ef7a9cd154805"


def _run_record(index: int, plan: str) -> dict[str, object]:
    day = index % 28 + 1
    return {
        "run_id": f"r-20260101T0000{index:05d}-node-{index}",
        "project": PROJECT,
        "node": f"a-one-plan-ledger-read-{index:05d}",
        "role": "implement",
        "plan": plan,
        "section": "s5",
        "gate": "passed",
        "dispatched_at": f"2026-01-{day:02d}T00:00:00Z",
        "completed_at": f"2026-01-{day:02d}T00:00:10Z",
    }


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A store of 2,000 runs across 50 plans, written through the real writer."""
    root = tmp_path_factory.mktemp("one-plan-store")
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    for index in range(PLAN_COUNT * RUNS_PER_PLAN):
        ledger.write_run_file(
            PROJECT,
            _run_record(index, f"plan-{index % PLAN_COUNT:02d}"),
            root,
            fsync=False,
        )
    # Build the index once, so the counted reads below are warm reads: a cold
    # read has to decode the documents it is indexing, which is not the claim.
    ledger.runs(PROJECT, root)
    return root


@contextlib.contextmanager
def _decoded_plans():
    """Every payload a block decodes as a run, by the plan the run names."""
    seen: list[str] = []
    real = json.loads

    def counting(payload, *args, **kwargs):
        value = real(payload, *args, **kwargs)
        if isinstance(value, dict) and "run_id" in value:
            seen.append(str(value.get("plan") or ""))
        return value

    with mock.patch.object(ledger.json, "loads", counting):
        yield seen


def test_a_one_plan_read_decodes_no_other_plans_documents(store: Path) -> None:
    """The measured read costs its own plan's payloads and no others."""
    with _decoded_plans() as decoded:
        rows, _version = ledger.runs(PROJECT, store, plan=SELECTED)

    assert len(rows) == RUNS_PER_PLAN, (
        f"the read returns the plan's own rows: {len(rows)} of {RUNS_PER_PLAN}"
    )
    others = sorted({plan for plan in decoded if plan and plan != SELECTED})
    assert not others, (
        "a one-plan read decoded other plans' run documents: "
        f"{len(others)} plans, first {others[:5]}"
    )


def test_the_unfiltered_read_decodes_every_plan(store: Path) -> None:
    """The positive control: the instrument sees every plan when unfiltered."""
    with _decoded_plans() as decoded:
        rows, _version = ledger.runs(PROJECT, store)

    assert len(rows) == PLAN_COUNT * RUNS_PER_PLAN
    seen = {plan for plan in decoded if plan}
    assert len(seen) == PLAN_COUNT, (
        "the counter must see every plan present before an absence is reported: "
        f"{len(seen)} of {PLAN_COUNT}"
    )


def test_the_filtered_read_is_the_unfiltered_read_filtered(store: Path) -> None:
    """Pushing the filter into the query changes which rows cost, not which return."""
    filtered, _filtered_version = ledger.runs(PROJECT, store, plan=SELECTED)
    everything, _version = ledger.runs(PROJECT, store)
    expected = [
        record for record in everything if str(record.get("plan") or "") == SELECTED
    ]
    assert filtered == expected, "the same rows, in the same order"
    assert len(filtered) == RUNS_PER_PLAN
