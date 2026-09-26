"""A stalled row says whether the worker's process is alive.

Two situations land on the one word ``stalled`` and their remedies are
opposite: a live worker in a long quiet step needs nothing, while a gone one
needs a resume. Measured 2026-09-22, four coordinators had to check the process
and the stream by hand to tell them apart, and a resume offered to a live
worker invites an intervention that destroys the work it was meant to rescue.

Both rows here are driven through the classifier and the published watch line:
``_watch_snapshot`` reduces the pointer, ``_watch_transition`` builds the event
the events log persists, and ``format_watch_transition`` renders it through the
ticker's own reason clause. The clause is asserted on the rendered row rather
than on a string composed in this file, because the pane is where a coordinator
reads it.

The declared mutation deletes the process-state clause from the stalled detail
and leaves the quiet time alone; the live-process case must then fail on its
own assertion, ``"alive" in line``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew import ticker as ticker_module
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "delete the process-state clause from the stalled detail; the live-process "
    "case must fail on the alive assertion"
)

# The window these stub runs are judged against, in seconds.
STALL_SECONDS = 900

# More than two minutes past the window, and not a whole number of minutes of
# quiet, so a row naming the wrong unit fails rather than reading plausibly.
QUIET_SECONDS = 1919

# The pane these rows render at: the widened grid the workstation measures,
# where the reason clause keeps its whole width.
PANE_WIDTH = 208

# A stream tail that never completed a record: the worker was killed mid-write,
# so there is no last record to read and the stall check is what sees it.
TRUNCATED_TAIL = '{"type":"assistant","message":{"content":[{"text":"half a rec'

# The run ids the two cases carry. The rendered row prints the run id in its
# node cell, so an id spelling one of the words under test would satisfy that
# case's own assertion from the node name rather than from the clause — the
# check would pass at a head that says nothing about the process at all.
LIVE_RUN_ID = "r-quiet-held"
GONE_RUN_ID = "r-quiet-vacant"

IN_PROGRESS_MANIFEST = "node: {run_id}\nstatus: in-progress\n"


def _home_fingerprint(home: Path) -> list[tuple[str, int]]:
    """The real config home's own entries, by name and mtime.

    One directory level only: the point is to catch a write that landed in the
    reader's own home, and a recursive walk of a live fleet's home on GPFS is
    the crawl this check must not itself become.
    """
    if not home.is_dir():
        return []
    return sorted((entry.name, entry.stat().st_mtime_ns) for entry in home.iterdir())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against a temporary config home, and prove the real one untouched.

    The fixture is the receipt for its own isolation: a pointer or a run
    directory written to the real home would both escape the test and collide
    with a live fleet, and the fingerprint is read before the environment moves
    so a write anywhere the harness reads is visible afterwards.
    """
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    # The isolation is asserted rather than assumed: a fixture that failed to
    # redirect the home would let every write below land in the real one.
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert _home_fingerprint(real_home) == before


def _stub_pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int,
    stream_text: str = TRUNCATED_TAIL,
) -> tuple[dict, float]:
    """One quiet, non-terminal run, told apart by its process alone.

    The manifest is the worker's own non-terminal last word beside the stall,
    which is what keeps the classifier's reading of a dead process out of the
    abandoned bucket and on the stall path — the reported case where a dead
    worker read as nothing and then as stalled.
    """
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=pid,
        phase="working",
        manifest_body=IN_PROGRESS_MANIFEST.format(run_id=run_id),
        write_stream=False,
    )
    moment = time.time()
    stream = Path(pointer["log_path"])
    stream.write_text(stream_text, encoding="utf-8")
    quiet_at = moment - QUIET_SECONDS
    os.utime(stream, (quiet_at, quiet_at))
    return pointer, moment


def _render_stalled_row(pointer: dict, moment: float) -> tuple[dict, dict, str]:
    """The classifier's reading and the watch line a coordinator sees."""
    row = recovery.classify_pointer(
        pointer, now_seconds=moment, stale_after_seconds=STALL_SECONDS
    )
    verdict = row["fleet_verdict"]
    assert verdict["state"] == "stalled", verdict["detail"]
    snapshot = recovery._watch_snapshot(
        pointer, moment=moment, stall_seconds=STALL_SECONDS
    )
    assert snapshot["state"] == "stalled", snapshot["detail"]
    transition = recovery._watch_transition(
        "liveness-fixture",
        kind="stalled",
        snapshot=snapshot,
        previous="working",
        current=str(snapshot["state"]),
        counts=recovery._fleet_counts({str(snapshot["run_id"]): snapshot}),
    )
    line = recovery.format_watch_transition(
        transition, ticker=ticker_module.Ticker(width=PANE_WIDTH, color=False)
    )
    return row, snapshot, line


def _named_minutes(line: str) -> int:
    named = re.search(r"quiet (\d+)m\b", line)
    assert named is not None, line
    return int(named.group(1))


def test_a_stalled_row_names_a_live_process(tmp_path: Path) -> None:
    """The control that a colour or a glyph cannot carry: the process lives.

    A live worker quiet past the window is the case a resume would harm, so the
    row has to say so in words a reader does not have to decode, and name how
    long it has been quiet.
    """
    with liveness._live_child() as pid:
        pointer, moment = _stub_pointer(tmp_path, LIVE_RUN_ID, pid=pid)
        row, snapshot, line = _render_stalled_row(pointer, moment)
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)

    assert quiet > STALL_SECONDS, quiet
    assert row["process_alive"] is True
    assert snapshot["process_alive"] is True
    assert "alive" in line, line
    assert "process gone" not in line, line
    assert row["fleet_verdict"]["detail"].startswith("alive, quiet "), row["detail"]
    assert _named_minutes(line) == quiet // 60, line


def test_a_stalled_row_names_a_gone_process(tmp_path: Path) -> None:
    """The other side of the same word: the worker's process is gone.

    Nothing is alive to resume into, so the row has to say that plainly — the
    reading a coordinator acts on, and the one the stall census found in 97 of
    118 stalls.
    """
    pointer, moment = _stub_pointer(tmp_path, GONE_RUN_ID, pid=liveness._absent_pid())
    row, snapshot, line = _render_stalled_row(pointer, moment)
    quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)

    assert quiet > STALL_SECONDS, quiet
    assert row["process_alive"] is False
    assert snapshot["process_alive"] is False
    assert "process gone" in line, line
    assert "alive" not in line, line
    assert row["fleet_verdict"]["detail"].startswith("process gone, quiet "), row[
        "detail"
    ]
    assert _named_minutes(line) == quiet // 60, line
