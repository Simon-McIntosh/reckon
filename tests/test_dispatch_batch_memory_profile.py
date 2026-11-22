"""The batch's section-hold read costs one node's rows, not the store's size.

A ``crew dispatch --batch`` asks, once per entry, whether the node it is about
to queue has already been delivered. The answer lives in the run store's
promoted rows. Reading them all — the whole project's history, each row decoded
with the wide detail a promotion records — makes the batch's peak resident
memory grow with the store rather than with the work it was asked to queue, and
a queueing coordinator was measured holding several gigabytes to place eight
entries.

The read is answered here from the store's own node index and projected to the
few fields the hold decision turns on, so queuing one node decodes that node's
rows and the wide detail of every other run is never brought into the process.
The two cases below measure the rows actually decoded, through the store's one
decode point, rather than the rows a caller is handed: a projection that still
decoded the population and then dropped it would pass a test on its answer and
fail the measure this node exists for.

Both cases are written to fail against the read this module replaced: it asked
the ledger for the project's whole picker projection and filtered in Python, so
every case below decoded the full population instead of the named node's rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon import run_store
from reckon.crew import section_holds

PROJECT = "demo"
PLAN = "plan-target"
SECTION = "s1"
#: The node the caller is about to queue: three promotions have taken it.
NODE = "plan-target-s1"
#: Rows for other nodes of the same plan and project. They are the population
#: the read must walk past without decoding.
FOREIGN = 600

NOW = "2026-11-22T00:00:00Z"


def _record(run_id: str, *, node: str, section: str = SECTION) -> dict[str, Any]:
    """One promoted row with the wide detail a real promotion records."""

    return {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker",
        "node": node,
        "plan": PLAN,
        "section": section,
        "role": "implement",
        "gate": "passed",
        "dispatched_at": "2026-11-21T23:30:00Z",
        "completed_at": NOW,
        # The wide detail lives in the run_details half. A read that decodes it
        # brings kilobytes per row into the process; the hold decision uses none
        # of it.
        "node_definition": {"id": node, "goal": "g" * 4000, "write_paths": ["x" * 500]},
        "budget": {"sent_tokens": 123456, "notes": "b" * 4000},
        "throughput": {"tokens_per_second": 12.5, "trace": ["t" * 200] * 20},
        "lane_receipt": {"lane": "local", "detail": "r" * 4000},
        "gate_check": {"verdict": "passed", "output": "o" * 4000},
    }


def _seed(rows: int) -> None:
    """Write the target node's promotions and a population of foreign rows."""

    store = run_store.RunStore(run_store.store_path())
    for index in range(3):
        store.append(PROJECT, _record(f"r-target-{index}", node=NODE))
    for index in range(rows):
        store.append(
            PROJECT,
            _record(f"r-foreign-{index:04d}", node=f"other-node-{index:04d}"),
        )


def _decoded_rows(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the rows the store decodes, through its one decode point."""

    decoded: list[str] = []
    owner = run_store._decode_run_row

    def recording(payload: str, detail: str | None) -> dict[str, Any]:
        decoded.append(str(len(payload)))
        return owner(payload, detail)

    monkeypatch.setattr(run_store, "_decode_run_row", recording)
    return decoded


def _reset_history() -> None:
    section_holds._HISTORY_READS.clear()
    run_store._HELD_ANSWERS.clear()


def test_queueing_one_node_decodes_that_node_not_the_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One node's hold answer decodes its own rows and none of the population."""

    _seed(FOREIGN)
    _reset_history()
    decoded = _decoded_rows(monkeypatch)

    held = section_holds.held_node_rows(
        PROJECT, tmp_path, (), plan=PLAN, section=SECTION, node=NODE
    )

    # The hold is per node: the node's last promotion is the recorded hold.
    assert [row["run_id"] for row in held] == ["r-target-2"], (
        f"the target node's own promotion is not the hold: {held}"
    )
    assert len(decoded) == 3, (
        "queuing one node decoded rows beyond its own: "
        f"{len(decoded)} rows for a {FOREIGN + 3}-row store"
    )


def test_doubling_the_store_does_not_change_what_one_node_decodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read is bounded by the node asked about, not by the store's size."""

    _seed(FOREIGN)
    _reset_history()
    first = _decoded_rows(monkeypatch)
    section_holds.held_node_rows(
        PROJECT, tmp_path, (), plan=PLAN, section=SECTION, node=NODE
    )
    at_one_population = len(first)

    # Double the delivered-run rows the store holds, then ask the same question.
    store = run_store.RunStore(run_store.store_path())
    for index in range(FOREIGN):
        store.append(
            PROJECT,
            _record(f"r-extra-{index:04d}", node=f"extra-node-{index:04d}"),
        )
    _reset_history()
    del first[:]
    held = section_holds.held_node_rows(
        PROJECT, tmp_path, (), plan=PLAN, section=SECTION, node=NODE
    )

    assert len(held) == 1
    assert len(first) == at_one_population, (
        "doubling the store changed the rows one node's hold answer decodes: "
        f"{len(first)} rows against {at_one_population}"
    )