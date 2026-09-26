"""A stalled row says which of three process states the worker is in.

Three situations land on the one word ``stalled`` and their remedies differ: a
live worker in a long quiet step needs nothing, a worker a check on this host
found dead needs a resume, and a worker whose liveness was never established
needs the check a reader would otherwise run by hand. Measured 2026-09-22, four
coordinators had to check the process and the stream by hand to tell them apart,
and a check run by hand is the cost this row exists to remove.

Every row here is driven through the classifier and the published watch line:
``_watch_snapshot`` reduces the pointer, ``_watch_transition`` builds the event
the events log persists, and ``format_watch_transition`` renders it through the
ticker's own reason clause. The clause is asserted on the rendered row rather
than on a string composed in this file, because the pane is where a coordinator
reads it.

The minutes a row names are asserted against the fixture's own stream
timestamp, never against the reading the renderer took. Comparing the row with
``_run_stream_quiet_*`` would put both sides of the comparison on one clock, so
a regression inside that reader would move them together and leave this file
green; read from the fixture instead, the two disagree the moment the reader
drifts.

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

# The run ids the cases carry. The rendered row prints the run id in its node
# cell, so an id spelling one of the words under test would satisfy that case's
# own assertion from the node name rather than from the clause, and the check
# would pass at a head that says nothing about the process at all.
LIVE_RUN_ID = "r-quiet-held"
GONE_RUN_ID = "r-quiet-vacant"
UNRECORDED_RUN_ID = "r-quiet-unlogged"
FOREIGN_RUN_ID = "r-quiet-earlier"

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
    with a live fleet, so the fingerprint is taken before the environment moves
    and the same home is re-read after the case ends.
    """
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert _home_fingerprint(real_home) == before


def _fixture_quiet_seconds(pointer: dict, moment: float) -> int:
    """The quiet time the fixture itself wrote, from the stream's own mtime.

    Deliberately not the reader the renderer calls: an expectation taken from
    that reader moves with it, so a regression inside it would leave the row
    and its expectation drifting together and this assertion green.
    """
    return int(moment - os.stat(pointer["log_path"]).st_mtime)


def _stub_pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int | None,
    stream_text: str = TRUNCATED_TAIL,
    launcher_host: str | None = None,
    stored_alive: bool | None = None,
) -> tuple[dict, float]:
    """One quiet, non-terminal run, told apart by what its process says.

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
    if launcher_host is not None:
        pointer["launcher_host"] = launcher_host
    if stored_alive is not None:
        pointer["process_alive"] = stored_alive
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
        quiet = _fixture_quiet_seconds(pointer, moment)

    assert quiet > STALL_SECONDS, quiet
    assert row["process_alive"] is True
    assert snapshot["process_alive"] is True
    assert "alive" in line, line
    assert "process gone" not in line, line
    assert row["fleet_verdict"]["detail"].startswith("alive, quiet "), row["detail"]
    assert _named_minutes(line) == quiet // 60, line


def test_a_stalled_row_names_a_gone_process(tmp_path: Path) -> None:
    """The other side of the same word: a check here found the process gone.

    The pid is a real number far above the kernel's ceiling, so the process
    table was asked on this host and answered that nothing holds it — the
    observation that licenses the row to say a process is gone.
    """
    pointer, moment = _stub_pointer(tmp_path, GONE_RUN_ID, pid=liveness._absent_pid())
    row, snapshot, line = _render_stalled_row(pointer, moment)
    quiet = _fixture_quiet_seconds(pointer, moment)

    assert quiet > STALL_SECONDS, quiet
    assert row["process_alive"] is False
    assert snapshot["process_alive"] is False
    assert row["liveness_proven"] is True
    assert "process gone" in line, line
    assert "alive" not in line, line
    assert row["fleet_verdict"]["detail"].startswith("process gone, quiet "), row[
        "detail"
    ]
    assert _named_minutes(line) == quiet // 60, line


def test_a_stalled_row_admits_an_unrecorded_process(tmp_path: Path) -> None:
    """No pid was recorded, so nothing observed the process either way.

    The row's whole purpose is to be acted on without the hand-check it
    replaces, and calling this case dead is that check's most expensive
    misreading: it reports an observation no one took, and a coordinator acts
    on it by resuming the run.
    """
    pointer, moment = _stub_pointer(tmp_path, UNRECORDED_RUN_ID, pid=None)
    row, snapshot, line = _render_stalled_row(pointer, moment)
    quiet = _fixture_quiet_seconds(pointer, moment)

    assert quiet > STALL_SECONDS, quiet
    assert row["process_alive"] is None
    assert snapshot["process_alive"] is None
    assert row["liveness_proven"] is False
    assert "liveness unknown" in line, line
    assert "process gone" not in line, line
    assert row["fleet_verdict"]["detail"].startswith("liveness unknown, quiet "), row[
        "detail"
    ]
    assert _named_minutes(line) == quiet // 60, line


def test_a_stalled_row_admits_a_process_it_cannot_check(tmp_path: Path) -> None:
    """Another machine launched it, so this host cannot ask about the pid.

    The stored answer came from a pointer written elsewhere and no reading was
    taken here, which makes it no reading at all for this row's purpose; a
    stale value a reader cannot see the provenance of would otherwise decide
    the remedy.
    """
    pointer, moment = _stub_pointer(
        tmp_path,
        FOREIGN_RUN_ID,
        pid=liveness._absent_pid(),
        launcher_host="a-different-login-node",
        stored_alive=False,
    )
    row, snapshot, line = _render_stalled_row(pointer, moment)
    quiet = _fixture_quiet_seconds(pointer, moment)

    assert quiet > STALL_SECONDS, quiet
    assert row["process_alive"] is False
    assert snapshot["process_alive"] is False
    assert row["liveness_proven"] is False
    assert "liveness unknown" in line, line
    assert "process gone" not in line, line
    assert _named_minutes(line) == quiet // 60, line
