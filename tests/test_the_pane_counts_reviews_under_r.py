"""A review is counted under its own ``r``, and a run being repaired is working.

The counters are how a coordinator reads a session at a glance: a session whose
only live run is a review printed ``0w``, which reads as an idle session when a
review is running. The working figure is therefore split by the role the run
was launched with, and the review work gets the pane word ``in review`` and the
letter ``r``, drawn after ``w``.

A run the review sent back into a repair round keeps the verdict it delivered
until it delivers again, so counting its state alone put a repairing run under
``d``: the reader saw delivered work with no worker on it. A repair round that
holds a live attempt is working again, and it counts under the working figure.

A requested promotion is not the same fact either: a delivered run waits on the
coordinator, while a promotion request waits on the queue that runs it. That is
its own cell, drawn after ``q`` and before the stuck figure, so the pane never
reads requested work as work nobody has asked for.

These cases go through the producer's own partition and transition builder, so
each figure asserted here is the figure a pane receives rather than a fixture's
opinion of one.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs
from reckon.crew import ticker as ticker_module
from reckon.crew.recovery import _fleet_counts, _watch_transition
from reckon.crew.recovery_review_dispatch import REPAIR_DISPATCH_FIELD

WIDTH = 180


def _snapshot(
    run_id: str,
    node: str,
    state: str,
    *,
    role: str = "implement",
    repair_in_flight: bool = False,
) -> dict[str, Any]:
    """One live pointer reduced to the facts the partition and the row read."""
    return {
        "run_id": run_id,
        "node": node,
        "state": state,
        "detail": "",
        "recovery_classification": state,
        "recovery": "",
        "role": role,
        "repair_in_flight": repair_in_flight,
        "model": "dsv4-flash",
        "effort": "high",
        "alias": "",
        "session": "",
    }


def _reading(*snapshots: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """The fleet's figures and one row per snapshot, through the producer."""
    counts = _fleet_counts({snapshot["run_id"]: snapshot for snapshot in snapshots})
    rows = [
        _watch_transition(
            "proj",
            kind="baseline",
            snapshot=snapshot,
            previous=None,
            current=snapshot["state"],
            counts=counts,
            spend_runs=[],
            rate_statuses={},
        )
        for snapshot in snapshots
    ]
    return counts, rows


def _render(row: dict[str, Any]) -> str:
    return ticker_module.Ticker(width=WIDTH).render(row)


def test_one_implementation_and_one_review_count_1w_1r() -> None:
    """A working review is counted under ``r`` beside the implementation's ``w``.

    This is the reading the counter exists for: the review is live work, and a
    single working figure would report the session as having one worker without
    saying whether that review or implemented work was the one running.
    """
    counts, rows = _reading(
        _snapshot("r-impl", "north", "working"),
        _snapshot("r-review", "review-of-r-impl-abc123", "working", role="review"),
    )
    assert counts["working"] == 1, counts
    assert counts["reviewing"] == 1, counts

    line = _render(rows[0])
    assert re.search(r"\b1w\s+1r\b", line), line

    # A fleet whose only live run is a review reads as reviewing, not idle.
    review_only, _ = _reading(
        _snapshot("r-review", "review-of-r-impl-abc123", "running", role="review")
    )
    assert review_only["working"] == 0, review_only
    assert review_only["reviewing"] == 1, review_only


def test_a_delivered_run_with_a_live_repair_attempt_counts_as_working() -> None:
    """A repairing run is working, not delivered.

    The review sent the run back to work, and its manifest keeps the verdict it
    delivered until the repair delivers again. Counting the state alone reports
    it as delivered, which reads as work waiting on the coordinator rather than
    work in hand.
    """
    counts, rows = _reading(
        _snapshot("r-repair", "north", "complete", repair_in_flight=True)
    )
    assert counts["working"] == 1, counts
    assert counts["unpromoted"] == 0, counts

    line = _render(rows[0])
    assert "1w" in line, line
    assert " 0d" in line, line

    # A delivered run with no live attempt is still delivered.
    idle, _ = _reading(_snapshot("r-done", "south", "complete"))
    assert idle["working"] == 0, idle
    assert idle["unpromoted"] == 1, idle


def test_the_row_carries_seven_right_aligned_cells_in_the_order_w_r_h_d_q_p_s() -> None:
    """The row renders seven counter cells, letters in order, at one fixed width.

    Every cell renders even on a fleet holding no review and no stuck request,
    so the block never changes shape as that work starts and stops and the
    clause after it keeps its column. Each count is right-aligned to the
    block's fixed digit width, so the letters hold their columns across one-
    and two-digit fleets. The promotion queue's cell carries the oldest
    request's position beside its count, because a queue figure alone does not
    say how long the head of it has waited.
    """
    counts, rows = _reading(
        _snapshot("r-work", "north", "working"),
        _snapshot("r-review", "review-of-r-work-abc123", "working", role="review"),
    )
    line = _render(rows[0])
    assert re.findall(r"\d+([wrhdqps])", line) == [
        "w",
        "r",
        "h",
        "d",
        "q",
        "p",
        "s",
    ], line

    plain, plain_rows = _reading(_snapshot("r-work", "north", "working"))
    assert plain.get("reviewing", 0) == 0
    assert re.findall(r"\d+([wrhdqps])", _render(plain_rows[0])) == [
        "w",
        "r",
        "h",
        "d",
        "q",
        "p",
        "s",
    ]

    assert ticker_module._MAX_CELLS == (
        "working",
        "in review",
        "help",
        "delivered",
        "waiting",
        "promotion queue",
        "stuck",
    )
    assert ticker_module._COUNT_FIELD["in review"] == "reviewing"
    assert ticker_module._COUNT_FIELD["promotion queue"] == "promotion_queued"
    assert ticker_module.STAT_LETTER["in review"] == "r"
    assert ticker_module.STAT_LETTER["promotion queue"] == "p"

    digits = ticker_module.STAT_DIGITS
    cells = ticker_module.Ticker()._stats(
        {
            "working": 3,
            "reviewing": 2,
            "blocked": 1,
            "unpromoted": 7,
            "waiting": 4,
            "promotion_queued": 6,
            "oldest_promotion_position": 5,
            "stuck": 5,
        }
    )
    texts = [text for text, _ in cells if text != " "]
    assert texts == [
        f"{3:>{digits}}w",
        f"{2:>{digits}}r",
        f"{1:>{digits}}h",
        f"{7:>{digits}}d",
        f"{4:>{digits}}q",
        f"{6:>{digits}}p@{5:>{digits}}",
        f"{5:>{digits}}s",
    ]
    assert len("".join(text for text, _ in cells)) == ticker_module.STATS
    assert counts["reviewing"] == 1


PROJECT = "pane-counts"
FOREIGN_HOST = "a-login-node-that-is-not-this-one"
MOMENT = 1_800_000_000.0
STALL_SECONDS = 60
# A run record the reflex never stamped a repair round on, so a case can say
# "no field at all" apart from any value the field could hold.
ABSENT = object()


def _seed(run_id: str, repair: Any) -> dict[str, Any]:
    """Write one live working run whose record carries ``repair``.

    The run is written to disk as well as returned, so the snapshot is built
    from the same files a real pointer resolves through.
    """
    started = datetime.fromtimestamp(MOMENT - 30, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": f"node-{run_id}", "plan": "plan-a"},
        "phase": "working",
        "role": "implement",
        # A foreign-launching host keeps the stored liveness reading rather than
        # probing this host's process table, so the fixture needs no live pid.
        "launcher_host": FOREIGN_HOST,
        "process_alive": True,
        "backend": "local",
        "launch": "cli",
        "command": "reckon",
        "manifest_path": str(directory / "manifest.md"),
        "log_path": str(directory / "stream.jsonl"),
        "status": "running",
        "attempt_started_at": started,
    }
    if repair is not ABSENT:
        record[REPAIR_DISPATCH_FIELD] = repair
    Path(record["manifest_path"]).write_text(
        f"---\nnode: {run_id}\nstatus: running\n---\n\nbody\n", encoding="utf-8"
    )
    stream = Path(record["log_path"])
    stream.write_text("", encoding="utf-8")
    os.utime(stream, (MOMENT, MOMENT))
    pointer = runs.pointer_path(run_id)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(record))
    return record


@pytest.mark.parametrize(
    ("label", "repair", "live"),
    [
        ("resumed", {"status": "resumed", "round_id": "r-abc@deadbeef"}, True),
        ("dispatched", {"status": "dispatched", "round_id": "r-abc@deadbeef"}, True),
        ("refused", {"status": "refused", "round_id": "r-abc@deadbeef"}, False),
        ("held", {"status": "held", "round_id": "r-abc@deadbeef"}, False),
        ("opening-without-a-round", {"status": "resumed", "round_id": ""}, False),
        ("not-a-mapping", "not a mapping", False),
        ("absent", ABSENT, False),
    ],
)
def test_the_snapshot_reads_a_live_repair_attempt_from_the_run_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, repair: Any, live: bool
) -> None:
    """Only an opening repair record with a round identity is a live attempt.

    The reading is taken from a real run record through the snapshot builder,
    because the counter reads the field that builder writes: a fixture that
    supplied the field itself would stay green if the builder stopped writing
    it. Which records count as open is the reflex's own answer, asked of its
    owner rather than restated here, so a record the owner refuses is refused
    by the snapshot too.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    recovery._SNAPSHOT_CACHE.clear()
    record = _seed(f"r-record-{label}", repair)

    snapshot = recovery._compute_watch_snapshot(
        record, moment=MOMENT, stall_seconds=STALL_SECONDS
    )

    assert snapshot["repair_in_flight"] is live
